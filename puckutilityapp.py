#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
# Dependencies:
#   - python3 -m pip install wxpython canopen semver uptime
#   - Peak PCAN USB hardware
#
# If you are using Windows, you'll need to install PCAN Basic >= 4.2.
#
# If you are using Python >= 3.8 on Linux, you might need to make these changes to wxpython:
# https://github.com/wxGlade/wxGlade/commit/e866729f91363a9c16cc6595e3424a0df817e048


import wx
from puckutilityapp_gui import puckutilityapp_frame
from calibrate_menu import calibrate
from factory_menu import factory
import OnOffButton
import widgets

import os
import canopen
import platform
import time
import subprocess
import math
import semver
from threading import Thread
import multiprocessing
multiprocessing.freeze_support() 
from canopen_runner import progressbar
from flashp4 import progressbar
import time
import webbrowser
import sys
import math
import datetime
import canopen_runner
import flashp4
import threading
import wx.lib.agw.pygauge as PG
import argparse
import configparser

# TODO
# Need to detect faults and automatically setup the app back into idle!
# If gainfactor is 0 don't run calc and fail
# Set cal / config required flag if going from v3 -> v4 or reverse
# Setup confirmation of Puck type prior to configuring and raise error if not a match
# Auto focus when coming out of disabled??
# Add hotkeys to help guide! ******************************
# Look into possible issues with Pucks responding to sync messages when not in focus (this appears to be caused by COB ID only being updated when configuration is set)
# SET FLAG ^^ IF ID is changed so that config must be reuploaded
# SHOULD use RPDOs to handle control mode in the future and command values! This is the correct way to handle (needs an issue and addition for v1.1.5)
# Do not clear tpdo 1 and 2, use these in the monitor / position # THIS WOULD BE LOVELY *************
# Refresh looks awful on windows # ehhh not much we can do here
# Calibration steps individually still popup issue for multiple cal
# Firmware update to flashp4.py to program multiple pucks at once?? - nice to have 
# Need to update menu bar to include hotkeys
# WIDEN ERROR BOUNDS FOR CAL
# Controlling Play/Pause from the menu does NOT change the button state

def get_version(vers): # Convert uint32_t to semantic version: Major.Minor.Patch
    return "{0}.{1}.{2}".format(
        (vers >> 24) & 0xFF, (vers >> 8) & 0xFFFF, (vers & 0xFF))

def is_jlink_detected():
    if platform.system() == "Windows":
        jlink_app = "JLink.exe"
        return False # For now, until we can suppress JLink's Windows GUI
    elif platform.system() == "Linux":
        jlink_app =  "JLinkExe"

    string_to_find = "Connecting to J-Link via USB...O.K."
    with subprocess.Popen([jlink_app, "-commandfile", "jlink_detect.txt"], 
                stdout=subprocess.PIPE, bufsize=1, universal_newlines=True) as p:
        for line in p.stdout:
            if line.find( string_to_find ) != -1:
                return True
    return False

# Class for DropTarger
class DropTarget(wx.FileDropTarget):
    def __init__(self,window):
        wx.FileDropTarget.__init__(self)
        self.window = window

    def OnDropFiles(self,x,y,filenames):
        for filepath in filenames:
            wx.CallAfter(self.window.ProcessDroppedFile, filepath)
        return True

# Build class, then set drop target as frame

# wxGlade auto-generates the puckutilityapp_frame's event handler stubs (in wxp3_glade.py).
# We are overriding these stubs with real event handler code here.
# I'd rather use XRC files, but wxGlade 0.9.3 isn't generating event handler bindings for menu items!
# This app's functions are grouped into separate files by responsibility:
#  - puckutilityapp.py: Basic configuration and operation
#  - calibrate_menu.py: Functions related to calibration
#  - factory_menu.py: Functions related to commissioning
# Functions from these files are merged into this class using Python's "mixin" ability.
# Fun fact: Python class "mixins" override from left to right, so put the base class on the right.

class MyFrame(calibrate, factory, puckutilityapp_frame): 
    def __init__(self, *args, **kwds):

        puckutilityapp_frame.__init__(self, *args, **kwds)
        self._replace_static_texts()
        icons = wx.Icon("images/BarrettLogo.png")
        self.SetBackgroundColour(wx.Colour(255,255,255))
        USE_BUFFERED_DC = True

        # Init drop target
        dt = DropTarget(self)
        self.SetDropTarget(dt)

        # Initialize self variables
        self.encoderResolution = 4096 # cts / revolution
        self.adcWasON = False
        self.lastMode = 0 
        self.firstRun = True
        self.lastPosRad = 0
        self.lastSysTime = 0
        self.y = 0
        self.dirCounter = 0
        #self.NoPattern = 0 # Future implementation to avoid no motor spazzing on dial / rpm count
        self.motorPresent = True
        self.init = True
        self.initialize = []
        self.ID = 0
        self.settingID = False
        self.NetworkActive = True
        self.Rescanning = False

        self.outputShaft = True

        self.ADC_ON = False

        self.progressbar_EN = True # False
        self.update = []

        # Extra Safety Flags
        self.requireCal = False
        self.requireConfig = False

        # Barrett colors
        self.blue = '#253B92'
        self.orange = '#FF7C1B'
        self.gray = '#8C8C8C'

        self.peak_factor = 0.75 # % Peak for Current Colors

        self.ctrlKey = False

        # Setup Window + Icon
        self.SetIcon(wx.Icon('images/BarrettIcon.png'))
        self.SetTitle("Puck Utility App - v1.2.0 - DEV")
        self.button_6.SetBackgroundColour(self.gray) # Initialize with gray button in idle
        self.Bind(wx.EVT_KEY_DOWN,self.onKeyDown)
        self.Bind(wx.EVT_KEY_UP,self.onKeyUp)
        self.Bind(wx.EVT_CLOSE, self.onCloseFrame)
        self.backgroundBMP = wx.Bitmap("images/Background.png") # recreating the BMP each rewrite causes massive lagging this is much better!
        # Bind backgound function to assign bitmap
        self.Bind(wx.EVT_ERASE_BACKGROUND, self.OnEraseBackground)

        # Add button to onoffpanel
        # MAY WANT TO INCREASE THE SIZE OF THIS IN CASE IT GIVES BETTER RESOLUTION
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.onoff1 = widgets.TransparentOnOffButton(self.onoffpanel, -1, size=(50, 34), initial=0, border=False, name="2")
        self.onoff1.Bind(OnOffButton.EVT_ON_OFF, self.on_off_adc)
        # Demonstrate individual control adjustments
        self.onoff1.SetOnColour(self.orange) # Barrett Orange
        self.onoff1.SetOnForegroundColour(self.gray) # Barrett Blue
        self.onoff1.SetOffColour(self.blue) # Barret Gray
        self.onoff1.SetOffForegroundColour(self.gray) # Barrett Blue
        self.onoff1.SetToolTip("ADC Monitor ON/OFF")
        sizer.Add(self.onoff1, 0, wx.ALIGN_CENTER)
        self.onoffpanel.SetSizer(sizer)
        self.onoffpanel.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.onoffpanel.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)
        self.onoffpanel.Bind(wx.EVT_PAINT, self._paint_onoffpanel)

        # Replace wx.StaticBitmap Dial with transparent version
        self._dial_base_img = wx.Image("images/dialnobgcroppedscaled.png")
        dial_bmp = wx.Bitmap(self._dial_base_img)
        new_dial = widgets.TransparentBitmap(self, wx.ID_ANY, dial_bmp)
        new_dial.SetMinSize(self.Dial.GetMinSize())
        dial_sizer = self.Dial.GetContainingSizer()
        if dial_sizer:
            dial_sizer.Replace(self.Dial, new_dial)
        self.Dial.Destroy()
        self.Dial = new_dial
        self.Layout()

        # Disable the unimplemented menu items
        menu = "Menu"
        for item in [#"Calibrate All", 
          "Current Sense Timing", "Current Sense Slope", "Encoder Direction",
          "Tune Gains...", "Save to CSV..."]:
          menu_item = self.frame_menubar.FindMenuItem(menu, item)
          self.frame_menubar.Enable(menu_item, False)

        menu = "Factory"
        for item in ["Initialize Puck", "Test All", "Test Flash", "Test RAM", "Test EEPROM", "Test Amplifier", "Test Encoder"]:
          menu_item = self.frame_menubar.FindMenuItem(menu, item)
          self.frame_menubar.Enable(menu_item, False)

        # Hide the "Factory" menu if JLink is not detected
        # if not is_jlink_detected():
        self.frame_menubar.Remove(self.frame_menubar.FindMenu("Factory"))

        # should be able to over ride and make rounded corners! radius = 10 or 12
        # attempt to add progress bar
        # self.progress = wx.Gauge(self.frame_statusbar, range=100, style=wx.GA_HORIZONTAL| wx.CENTER | wx.ALL) #wx.ALIGN_CENTER_VERTICAL)
        self.progress = PG.PyGauge(self.frame_statusbar, range=100, style=wx.ALIGN_CENTER_VERTICAL | wx.ALL)
        # self.progress.SetBarColour(self.orange)
        self.progress.SetBarGradient(('#FFFFFF',self.orange))
        # print(self.progress.GetBarGradient())
        # self.progress.SetBarGradient()
        # self.progress.Hide()
        # self.progress.SetBorderColor(wx.BLACK)
        self.dc = wx.ScreenDC()
        # Initial Positioniing
        self.RepositionGauge()

        # NOW need to work on pass the update thread into other programs??
        self.update_queue = multiprocessing.Queue()

    def _paint_onoffpanel(self, event):
        panel = self.onoffpanel
        dc = wx.PaintDC(panel)
        pos = self.ScreenToClient(panel.GetScreenPosition())
        dc.DrawBitmap(self.backgroundBMP, -pos.x, -pos.y)

    def _replace_static_texts(self):
        """Swap every wx.StaticText child with a TransparentText in-place."""
        instance_attrs = {id(getattr(self, a)): a
                         for a in ('VBus', 'PTemp', 'MTemp', 'Vrpm')
                         if isinstance(getattr(self, a, None), wx.StaticText)}
        for child in list(self.GetChildren()):
            if not isinstance(child, wx.StaticText):
                continue
            style = child.GetWindowStyle() & (wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_RIGHT)
            new = widgets.TransparentText(self, wx.ID_ANY, child.GetLabel(), style=style)
            new.SetFont(child.GetFont())
            new.SetForegroundColour(child.GetForegroundColour())
            new.SetMinSize(child.GetMinSize())
            sizer = child.GetContainingSizer()
            if sizer:
                sizer.Replace(child, new)
            attr = instance_attrs.get(id(child))
            if attr:
                setattr(self, attr, new)
            child.Destroy()
        self.Layout()

    def set_tool_tips(self,event):
        wx.ToolTip.SetDelay(3000)
        wx.ToolTip.SetReshow(3000)
        # if self.getMode() == 'Current':
            # Specifications
        print('Setting Tool Tips...')
        self.choice_port.SetToolTip('Select CAN port')
        self.button_1.SetToolTip('Scan to find all Pucks on the CAN bus')
        self.choice_id.SetToolTip('Select active Puck')
        self.text_version.SetToolTip('Firmware version of active Puck')
        self.button_8.SetToolTip('Update firmware for active Puck')
        self.button_10.SetToolTip('Browse for a new configuration file to upload')
        self.choice_test.SetToolTip('Select control mode')
        self.text_testvalue.SetToolTip('Input a command value for the control mode')
        self.button_6.SetToolTip('Send the command value to active Puck')
        self.Dial.SetToolTip('Displays output position')
        self.onoffpanel.SetToolTip('Turn ON/OFF ADC Monitor')
        self.button_2.SetToolTip('Set new Puck ID')
        self.text_id.SetToolTip('Input new Puck ID')

    #this may be unnecessary
    # def OnResize(self,event):
    #     self.RepositionGauge()
    #     event.Skip()

    def RepositionGauge(self):
        rect = self.frame_statusbar.GetFieldRect(1)
        # print('repo')
        # Get text width and add this to the start spot!!
        # get the actual in use text for width? or just make smaller so it doesn't block text?
        text = "Progress: 100%"
        width, height = self.dc.GetTextExtent(text)
        self.progress.SetPosition((rect.x + 20 + width, int(rect.y * 2 + 2)))
        self.progress.SetSize((rect.width - 6, rect.height - 8))

    def UpdateProgress(self,value):
        # print('called')
        if value > 100:
            value = 100
        self.progress.SetValue(value)

    def OnStartTask(self,event):
        # Set color
        # print('Start')
        # if self.progressbar_EN == True:
        self.frame_statusbar.SetStatusText(f"Progress: 0%",1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()
        if self.adcWasON == True: # THIS IS ALWAYs changing?? i think at least? either way, not picking up the difference
            self.progress.SetBarGradient(('#FFFFFF',self.blue))
            # print('on')
        else:
            self.progress.SetBarGradient(('#FFFFFF',self.orange))
            # print('off')
        
    def OnTaskComplete(self):
        # self.thread.join()
        self.progress.Hide()
        self.UpdateProgress(0)
        self.frame_statusbar.SetStatusText("Ready", 1)

    def UpdateUI(self,value):
        # print('update ui')
        # self.progress.Show()
        # self.progress.SetValue(value)
        self.frame_statusbar.SetStatusText(f"Progress: {value}%",1)
        self.UpdateProgress(value)
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()
        # self.Refresh()
        # self.Update()
        # force refresh to help windows?

    def ProcessDroppedFile(self,filepath):
        # print(filepath)
        root, extension = os.path.splitext(filepath)
        # print(extension)
        if extension == '.ebin' or extension == '.bin':
            print('P4 Firmware Detected...')
            self.browse_fw(None,filepath)
            # Run firmware upload
        elif extension == '.csv':
            print('Motor Configuration Detected...')
            # Run configuration upload
            self.file_to_p3(None,filepath)
        elif extension == '.ini':
            print('System Configuration Detected...')
            start = time.time()
            self.system_config(None,filepath)
            finish = time.time()
            time_elapsed = round(finish - start,2)
            print('System Configuration Complete! Time elapsed: {} seconds'.format(time_elapsed))
        else:
            print('Invalid file...')
            # add a popup
            msg = 'Invalid file type! \n\n\nExpected Extensions-\nFirmware: ".ebin"\nMotor Config: ".csv"\nSystem Config: ".ini"'
            dlg = wx.MessageDialog(None,msg,'Warning!', wx.ICON_WARNING)
            dlg.ShowModal()
            dlg.Destroy()

    def OnEraseBackground(self, evt):
        # yanked from ColourDB.py
        dc = evt.GetDC()

        if not dc:
            dc = wx.ClientDC(self)
            rect = self.GetUpdateRegion().GetBox()
            dc.SetClippingRect(rect)
        dc.Clear()
        dc.DrawBitmap(self.backgroundBMP, 0, 0)

    # HOTKEYS

    def onKeyUp(self,event):
        if event.GetKeyCode() == 308:
            self.ctrlKey = False
        else:
            event.Skip()

    def onKeyDown(self,event):
        # https://archie-adams.github.io/keyboard-shortcut-map-maker/ to make map!
        # print(event.GetKeyCode())# Use to print key code
        if event.GetKeyCode() == 27: # ESC
            self.onCloseFrame(None)
        elif event.GetKeyCode() == 308: # CTRL 
            self.ctrlKey = True
        elif self.ctrlKey == True and event.GetKeyCode() == 67: # CTRL-C = Cal
            self.calibrate_all(None)
        elif self.ctrlKey == True and event.GetKeyCode() == 85: # CTRL-U = Update # allow .ini system config files?
            self.update_all(None)
        elif self.ctrlKey == True and event.GetKeyCode() == 83: # CTRL-S = Scan 
            self.scan_pucks(None)
        elif self.ctrlKey == True and event.GetKeyCode() == 80: # CTRL-P = Play/Pause ADC Monitor
            # Trigger button 
            if self.ADC_ON == False:
                self.onoff1.SetValue(1)
            elif self.ADC_ON == True:
                self.onoff1.SetValue(0)
            # ON/OFF 
            self.on_off_adc(self)
        else:
            event.Skip()
            return
    
    def setID(self,i):
        self.ID = i

    def getID(self):
        return self.ID
    
    def configure_Puck(self):

        # Read and set gear ratio from object dictionary
        motor_rev = self.node.sdo.upload(0x6091,1)
        motor_rev = int.from_bytes(motor_rev, byteorder='little',signed=False)
        shaft_rev = self.node.sdo.upload(0x6091,2)
        shaft_rev = int.from_bytes(shaft_rev, byteorder='little',signed=False)
        self.gearRatio = motor_rev / shaft_rev

        self.i_cont = self.node.sdo.upload(0x3011,8)
        self.i_cont = int.from_bytes(self.i_cont, byteorder='little',signed=False)
        print('I_cont: {}'.format(self.i_cont))
        self.i_peak = self.node.sdo.upload(0x3011,9)
        self.i_peak = int.from_bytes(self.i_peak, byteorder='little',signed=False)

        self.temp_limit = self.node.sdo.upload(0x2384,9)
        self.temp_limit = int.from_bytes(self.temp_limit, byteorder='little',signed=False)

        self.temp_limited_current = self.node.sdo.upload(0x3025,3)
        self.temp_limited_current = int.from_bytes(self.temp_limited_current, byteorder='little',signed=False)

        # Get peak velocity
        self.peak_velocity = self.node.sdo.upload(0x6080,0)
        self.peak_velocity = int.from_bytes(self.peak_velocity, byteorder='little',signed=False)

        print('Gear Ratio determined: {}'.format(self.gearRatio))

        print("Reading PDOs...")
        try:
            self.node.tpdo.read()
            self.node.rpdo.read()
        except:
            pass

        # CLear the local copy of the PDO Configs
        print("Clearing PDOs...")
        for i in (1,2,3,4):
            self.node.tpdo[i].clear()
            self.node.rpdo[i].clear()

        # 8-bytes (64 bits) per PDO - make sure there is space for each data type || split PDOs to fit (can change sync timing per PDO as well)
        print("Configuring TPDO3 and TPDO4 for ADC Monitor...") 
        self.node.tpdo[2].add_variable('i2t','Value') # (0x3025,1) i2t Value - "Value" (16 bit)
        self.node.tpdo[3].add_variable('CurrentFeedback') # Iq - "CurrentFeedback" (16 bit)
        self.node.tpdo[3].add_variable('Amplifier','Temperature') # (0x3000,2) Puck Temp - "Temperature" (16 bit)
        self.node.tpdo[3].add_variable('Motor','Therm') # (0x3010,3) Motor Temp - "Therm" (16 bit)
        self.node.tpdo[4].add_variable('PositionFeedback') # (0x6064, 0) Position - "PositionFeedback" (32 bit)
        self.node.tpdo[4].add_variable('VelocityFeedback')# (0x606C,0) Velocity - "VelocityFeedback" (32 bit)
        self.node.tpdo[2].trans_type = 10 # TX on every 10th sync
        self.node.tpdo[2].enabled = True
        self.node.tpdo[3].trans_type = 10 # TX on every 10th sync
        self.node.tpdo[3].enabled = True
        self.node.tpdo[4].trans_type = 0 # TX on every sync
        self.node.tpdo[4].enabled = True

        print("Writing TPDO's...")
        try:
            self.node.tpdo.save()
            # Write the new (empty) RPDO config to the device
            self.node.rpdo.save()
        except:
            print("Failed to set up TPDO's. Disabling monitor...")
            pass

        # Each time we receive this PDO from the puck, execute a callback
        self.node.tpdo[2].add_callback(self.tpdo2_callback)
        self.node.tpdo[3].add_callback(self.tpdo3_callback)
        self.node.tpdo[4].add_callback(self.tpdo4_callback)

        self.node.sdo["HeartbeatPeriod"].raw = 0

    def tpdo1_callback(self, msg):
        global node

        # Call function to update Position / Velocity Data
        wx.CallAfter(self.getPosition)

    def tpdo2_callback(self, msg):
        global node

        # Call function to update ADC Monitor
        wx.CallAfter(self.getMonitor)

    def tpdo3_callback(self, msg):
        global node

        # Call function to update ADC Monitor
        wx.CallAfter(self.getMonitor)

    def tpdo4_callback(self, msg):
        global node

        # Call function to update Position / Velocity Data
        wx.CallAfter(self.getPosition)

    def can_port(self,event,skipADC=False):
        #print("Event handler 'can_port'")
        if skipADC == True:
            pass
        elif self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False
 
        try:
            self.network.disconnect() # Close any open networks
        except:
            pass

        print("Establishing a new network...")
        self.network = canopen.Network()
        can_device = self.choice_port.GetStringSelection()

        try:
            if platform.system() == "Windows":
                self.network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
                # self.network.connect(bustype='slcan', channel='COM7@128000', bitrate=1000000) # for SLCAN
            elif platform.system() == "Linux":
                self.network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)    
            elif platform.system() == "Darwin":
                self.network.connect(bustype='pcan', channel='PCAN_USBBUS1',bitrate=1000000) 
            # This will attempt to read an SDO from nodes 1 - 127
            self.network.scanner.reset()
            self.network.scanner.search()
        #   return True
        except Exception as e: 
            # print(e)
            if "buffer" in str(e) or "heavy" in str(e):  
                print('No Pucks Found') # Establish error for no pucks
                msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
            else: 
                print('No CAN device found!')
                msg = 'No CAN device found! \nCheck connection and try again'
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            # Try to clear out selection of select ID and set ID
            n = ''
            self.choice_id.SetItems([n])
            self.text_id.ChangeValue(str(n))
            dlg.Destroy()
            return False
        # We may need to wait a short while here to allow all nodes to respond
        time.sleep(0.05)
        if skipADC == True:
            pass
        elif self.adcWasON == True:
            self.on_off_adc(self)
            self.adcWasON = False
        return True

    def scan_pucks(self, event,selfCALL=False,skipADC=False):  # wxGlade: wxp3_frame.<event_handler>
        #print("Event handler 'scan_pucks'")
        #print(str(datetime.datetime.now()) + " Event handler 'scan_pucks'")
        
        # Set Mode to IDLE in case test is active
        if skipADC == True:
            pass
        elif self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        self.progress.Hide()

        self.frame_statusbar.SetStatusText("Scanning Pucks...", 1)
        self.frame_statusbar.Update()
        wx.Yield()
        
        try:
            if self.lastMode != 0:
                self.lastMode = 0 # Reset lastMode
                self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
                self.button_6.SetBackgroundColour(self.gray)
                self.button_6.SetLabel("Go")
                print("Idling...")

            # This will attempt to read an SDO from nodes 1 - 127
            self.network.scanner.reset()
            self.network.scanner.search()
            time.sleep(0.5)
            # print('made it here')
            for node_id in self.network.scanner.nodes:
                print("Found node %d!" % node_id) 
            scan_length = len(self.network.scanner.nodes)
            if(scan_length > 0):
                MyApp.updateNodes(self, self.network.scanner.nodes)
                # Populate the node choice list
                self.choice_id.SetItems([str(i) for i in self.network.scanner.nodes])
            if self.init:                   
                self.initialize = self.network.scanner.nodes              
                print('Initializing CAN bus...')
                init_length = len(self.initialize)
                if init_length > 0:
                    self.init = False
                    print('Success!')

            # If we found at least one, select the first
            if scan_length > 0:
                # print('here')
                if self.getID() == 0:
                    self.choice_id.SetSelection(self.getID()) # This is actually what sets the initial
                else:
                    # do some rescan if not in self scanner (THIS IS WHERE THE NOT IN LIST BUG OCCURS)
                    indexID = self.network.scanner.nodes.index(self.getID())
                    self.choice_id.SetSelection(indexID)
                self.select_id(None)
            else:
                self.text_id.ChangeValue('') # clear ID
                self.choice_id.SetItems([])
                try:
                    result = self.can_port(None)
                    # print(result)
                    if result == True:
                        if selfCALL == False:
                            self.scan_pucks(None,True)
                        else:
                            print('No Pucks Found') # Establish error for no pucks
                            msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                            dlg = wx.MessageDialog(None,msg)
                            dlg.ShowModal()
                            dlg.Destroy()
                            # Set ADC Monitor Button Off after lost connection
                            self.ADC_ON == False
                            self.onoff1.SetValue(0) # Only sets button off
                            # Reset MyApp nodes
                            MyApp.updateNodes(self, self.network.scanner.nodes)

                except:
                    # print('No Pucks Found') # Establish error for no pucks
                    # msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                    # dlg = wx.MessageDialog(None,msg)
                    # dlg.ShowModal()
                    # dlg.Destroy()
                    pass
                # self.choice_id.SetSelection(0)
            #print(str(datetime.datetime.now()) + " Complete!!!")
        except Exception as e: 
            # print('now fail')
            # print(e)
            if "buffer" in str(e) or "heavy" in str(e):  
                print('No Pucks Found') # Establish error for no pucks
                msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                dlg = wx.MessageDialog(None,msg)
                dlg.ShowModal()
                dlg.Destroy()
            else: 
                try:
                    result = self.can_port(None)
                    # print(result)
                    if result == True:
                        self.scan_pucks(None)
                except:
                    pass

        if skipADC == True:
            pass
        elif self.adcWasON == True:
            self.on_off_adc(self)
            self.adcWasON = False

        self.frame_statusbar.SetStatusText("Ready", 1)
        self.frame_statusbar.Update()
        wx.Yield()

    def select_id(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False:
            return False
        #print("Event handler 'select_id'")
        if self.firstRun:
            active = MyApp.getPucks(self)
            compare = []
            for element in MyApp.getNodes(self):
                if element not in active:
                    compare.append(element)
            if self.getID() == 0:
                self.setID(compare)
                node_idx = compare.index(min(compare)) # idx of lowest inactive puck
            else:
                self.initialize = MyApp.getNodes(self)
                node_idx = self.initialize.index(self.getID())
            node_id = self.initialize[node_idx]
            self.setID(node_id)
            self.firstRun = False
        else: # Not first run
            node_id = int(self.choice_id.GetString(self.choice_id.GetSelection()))
            # Popup error if Node is already active and not the selected frames current node
            if node_id in MyApp.getPucks(self) and self.settingID != True and node_id != self.getID():
                indexID = self.network.scanner.nodes.index(self.getID())
                self.choice_id.SetSelection(indexID)
                print('Node already active')
                msg = ('Node already active!')
                dlg = wx.MessageDialog(None,msg)
                dlg.ShowModal()
                dlg.Destroy()
                return
            else:
                if self.getID() in MyApp.getPucks(self): 
                    MyApp.removePuck(self,self.getID())    
                self.setID(node_id)
                MyApp.addPucks(self,self.getID())

        print("Selected node = {0}".format(node_id))
        if node_id not in self.network:
          # Add our canopen node along with its object dictionary (for parsing)
          print("Adding new node: {0}".format(node_id))
          self.node = self.network.add_node(node_id, 'puck4.eds')
        else:  
          self.node = self.network[node_id]

        self.text_id.ChangeValue(str(node_id))

        version = get_version(self.node.sdo['MfgSoftwareVersion'].raw)
        # IF version is 0.0.0 and node_id is 127 update to say flashloader / bootloader (imply not ready) then skip the below part to avoid issues
        self.text_version.ChangeValue(version)

        # if get mode != 0 (idle) then set the mode box to current mode
        # then get target for whatever mode and populate input field
        current_mode = self.node.sdo["SetModeOfOperation"].raw
        if(current_mode == 0):     
            # update select test to idle and input to 0
            self.choice_test.SetSelection(0)
            self.text_testvalue.SetValue('0')
        elif(current_mode == 4):
            # update select test to trq mode and update input to current target torque
            self.choice_test.SetSelection(1)
            input = self.node.sdo["TargetTorque"].raw # This is out of 1000% maximum, needs conversion
            rated_torque = self.node.sdo["RatedTorque"].raw
            cmd_value = input * rated_torque * self.gearRatio / 1000 # cmd_value * 1000 / (rated_torque * self.gearRatio) # Scale
            self.text_testvalue.SetValue(str(round(cmd_value)))
        elif(current_mode == 3):
            # update select test to velocity mode and update input to current target torque
            print('updating mode...')
            self.choice_test.SetSelection(2)
            input = self.node.sdo["TargetVelocity"].raw
            cmd_value = (input * 60) / (4096 * self.gearRatio) # ctspersec = cmd_value * 4096 / 60 * self.gearRatio
            self.text_testvalue.SetValue(str(round(cmd_value)))
        elif(current_mode == 1):
            # update select test to trq mode and update input to current target torque
            print('updating mode...')
            self.choice_test.SetSelection(3)
            # No good way to get last position update in degrees, and no real reason to have this
            self.text_testvalue.SetValue('0')
        elif(current_mode == 6):
            # update select test to trq mode and update input to current target torque
            print('updating mode...')
            self.choice_test.SetSelection(4)
            self.text_testvalue.SetValue('0')
        
        # may want to make this more centralized (like for loop to configure all at once)
        # print('Configure...')
        self.configure_Puck() # This makes sure all pucks are configured to remove bug with first round adc on turning puck idle

    def set_id(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False:
            return
        
        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        if self.lastMode != 0:
            self.lastMode = 0 # Reset lastMode
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
            self.button_6.SetBackgroundColour(self.gray)
            self.button_6.SetLabel("Go")
            print("Idling...")

        #print("Event handler 'set_id'")
        if int(self.text_id.GetValue()) in MyApp.getNodes(self): # self.network.scanner.nodes: # Try this with active nodes??
            # Error message - resets ID to active if error
            indexID = self.network.scanner.nodes.index(self.getID())
            msg = ('ID already in use!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return
        if int(self.text_id.GetValue()) < 1: # or int(self.text_id.GetValue()) > 127: # Try to stop 127 loop
            # Error message - resets ID to active if error
            indexID = self.network.scanner.nodes.index(self.getID())
            self.text_id.ChangeValue(str(self.getID()))
            msg = ('Invalid CAN ID!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return
        self.settingID = True
        node_id = int(self.text_id.GetValue())
        
        try:
            MyApp.removePuck(self, self.getID())
        except:
            pass
        
        self.network.scanner.nodes.remove(self.getID())
        self.setID(node_id) # update node
        MyApp.addPucks(self, self.getID())
        self.network.scanner.nodes.append(self.getID())
        # Set the new ID
        print('Setting new node...')
        self.node.sdo['NetCfg'].raw = node_id
        time.sleep(0.05)
        
        # Re-scan
        self.scan_pucks(None)
        
        self.settingID = False

        if self.adcWasON == True:
            self.on_off_adc(self)

    def browse_fw(self, event, path=False):  # wxGlade: wxp3_frame.<event_handler>
        #print("Event handler 'browse_fw'")
        if self.check_for_node() == False:
            return

        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
            # Set Go Color to Gray
            self.button_6.SetBackgroundColour(self.gray)

        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

        # LET flash program handle version!!

        # Determine bootloader version
        # 1 = Windows blhost.exe
        # 2 = Win/Lin flashp3.py

        # Ping: msgID = can_id, dlc = 2, data = [5A A6]
        # Wait 200 ms (for possible reboot)
        # SDO request 0x100A,0
        # -> Success, version = 2
        # -> Failure, version = 1
        
        # Response to ping commands:
        # - Bootloader v1 = [5A, A7]
        # - Bootloader v2 = RESET
        # - RSF5 firmware = RESET
        # - CANopen firmware = RESET

        # self.network.send_message(int(node_id), [0x5A, 0xA6]) # Ping command
        # time.sleep(0.2) # Wait for reboot
        # try:
        #     version = get_version(self.node.sdo['MfgSoftwareVersion'].raw)
        # except:
        #     version = get_version(1 << 24) # Assume version 1.0.0

        # print("Found bootloader version: {0}".format(version))
        # Not necessary anymore
        # if semver.match(version, '==1.0.0') and platform.system() != "Windows":
        #     msg = "To update firmware, please run this program under Windows."
        #     print(msg)
        #     wx.MessageBox(msg, 'Info', wx.OK | wx.ICON_INFORMATION)
        #     return
        if path == False:
            # File browser
            if platform.system() == "Windows":
                directory = '../firmware'
            else:
                directory = 'firmware/'

            # File browser
            with wx.FileDialog(self, "Select firmware file", directory, wildcard="BIN files (*.bin;*.ebin)|*.bin;*.ebin",
                          style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
                if fileDialog.ShowModal() == wx.ID_CANCEL:
                    # Transmit an NMT reboot command to this node
                    # print("Rebooting puck")
                    # self.network.send_message(0x0, [0x81, int(node_id)])
                    # time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
                    # # self.network.send_message(0x4, [self.LAUNCH, int(node_id)])
                    # self.configure_Puck()
                    if self.adcWasON == True:
                        self.on_off_adc(self)
                    return     # the user changed their mind
                # Proceed loading the file chosen by the user
                pathname = fileDialog.GetPath()
        else:
            pathname = path

        self.frame_statusbar.SetStatusText("Updating firmware... (~30 seconds)", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        # timeStart = time.time()

        # if semver.match(version, '==1.0.0'):
        #     l = ['blhost', '-p', can_device + "," + node_id, 'flash-erase-all']
        #     subprocess.call(l) # Note: this waits until the subprocess exits

        #     l = ['blhost', '-p', can_device + "," + node_id, 'write-memory', '0x8000', pathname]
        #     subprocess.call(l) # Note: this waits until the subprocess exits

        #     # blhost -p can0,1 reset
        #     # blhost -p can0,1 execute 0 0 0 (address, arg, stack)
        #     l = ['blhost', '-p', can_device + "," + node_id, 'reset']
        #     subprocess.call(l) # Note: this waits until the subprocess exits
        # else:
        #     if platform.system() == "Windows":
        #         python_name = "python"
        #     else:
        #         python_name = "python3"
        #     l = [python_name, "flashp4.py", can_device, node_id, pathname]
            
            # OG WAY -_-
            # subprocess.call(l) # Note: this waits until the subprocess exits

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

        # print("Writing OD entries")
        self.network.disconnect()

        # Using multithreading!
        self.OnStartTask(None) # need this to show!! 

        process = multiprocessing.Process(target=flashp4.start,args=(can_device, int(node_id),pathname,self.update_queue,))
        process.start()
        self.progress.Show()

        self.update = []
        self.update.clear()

        while True:
            try:
                self.update.append(self.update_queue.get_nowait())
                if self.update[-1] == "Pass" or self.update[-1] == "Fail" or self.update[-1] == "Done":
                    result = self.update[-1]
                    # print('process complete')
                    break
                else:
                    # print(f"Received update: {self.update[-1]}% complete")
                    self.UpdateUI(self.update[-1])
            except multiprocessing.queues.Empty:
                time.sleep(0.05)

        process.terminate()
        process.join()
        self.OnTaskComplete()

        print(result)

        # Re-scan
        self.can_port(None,True)
        self.scan_pucks(None,False,True)
        # timeFinish = round(time.time() - timeStart,2)
        # print('Time elapsed: {}'.format(timeFinish))

        time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.adcWasON == True:
            self.on_off_adc(self)

    def file_to_p3(self, event, path=False):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False:
            return
        #print("Event handler 'file_to_p3'")
        # If motor is not idled, idle
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
            # Set Go Color to Gray
            self.button_6.SetBackgroundColour(self.gray)

        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True

        if path == False:
            # print('no path')
            # File browser
            if platform.system() == "Windows":
                directory = '../config'
            else:
                directory = 'config/'

            with wx.FileDialog(self, "Open CANopen CSV file", directory, wildcard="CSV files (*.csv)|*.csv",
                          style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:

              if fileDialog.ShowModal() == wx.ID_CANCEL:
                  if self.adcWasON == True:
                    self.on_off_adc(self)
                  return     # the user changed their mind
              # Proceed loading the file chosen by the user
              pathname = fileDialog.GetPath()
        else:
            pathname = path

        self.frame_statusbar.SetStatusText("Updating Config...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

        print("Writing OD entries...")
        self.network.disconnect()

        # Using multithreading!
        self.OnStartTask(None) # need this to show!! 
        process = multiprocessing.Process(target=canopen_runner.start,args=(can_device, int(node_id),'puck4.eds',pathname,self.update_queue,))
        process.start()
        self.progress.Show()

        self.update = []

        while True:
            try:
                self.update.append(self.update_queue.get_nowait())
                if self.update[-1] == "Pass" or self.update[-1] == "Fail":
                    result = self.update[-1]
                    # print('process complete')
                    break
                else:
                    # print(f"Received update: {self.update[-1]}% complete")
                    self.UpdateUI(self.update[-1])
            except multiprocessing.queues.Empty:
                time.sleep(0.05)

        process.terminate()
        process.join()
        self.OnTaskComplete()

        print(result)
        
        if result == "Pass":
          print("Success!")
        else:
          print("Configuration file failed to upload...")
          msg = "Configuration file failed to upload..." \
          "\n\nDebug:" \
          "\n-Verify proper configuration file formatting" \
          "\n-Verify correct version of config file" \
          "\n-View terminal log for additional details"
          dlg = wx.MessageDialog(None,msg)
          dlg.ShowModal()
          dlg.Destroy()

        # THIS SEEMS LIKE IT SHOULDN'T HAPPEN HERE, use can_port / scan_pucks??

        print("Establishing a new network...")
        self.network = canopen.Network()

        if platform.system() == "Windows":
          self.network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
        elif platform.system() == "Linux":
          self.network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)
        self.node = self.network.add_node(int(node_id), 'puck4.eds')
        
        # Save all OD entries to EEPROM (takes about 0.55 sec)
        print("Saving OD entries")
        default_timeout = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
        canopen.sdo.SdoClient.RESPONSE_TIMEOUT = 1.0
        self.node.sdo['Save']['All'].raw = 0x65766173 # Key = 'SAVE'
        canopen.sdo.SdoClient.RESPONSE_TIMEOUT = default_timeout

        # Transmit an NMT reboot command to this node
        print("Rebooting puck")
        self.network.send_message(0x0, [0x81, int(node_id)])
        time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
        # self.network.send_message(0x4, [self.LAUNCH, int(node_id)])
        # cansend can0 67F#2F.11.34.01.04.00.00.00
        self.configure_Puck()
        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.adcWasON == True:
            self.on_off_adc(self)
            self.adcWasON = False

    def select_test(self, event):  # wxGlade: wxp3_frame.<event_handler>
        #print("Event handler 'select_test'")
        
        if len(self.network.scanner.nodes) == 0:
            # Error message if no Bus
            # reset selection
            self.choice_test.SetSelection(0)
            print('No active node!')
            msg = ('No active node!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return
        elif self.requireCal or self.requireConfig:
            print('Needs cal or config...')
            return
        
        quick_test = self.choice_test.GetSelection()
        if quick_test == 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
            # Set Go Color to Gray
            self.button_6.SetBackgroundColour(self.gray)
            return
        else:
            # Clear faults, RTSO, OpEnabled
            print("Going OpEnabled")
            self.node.sdo["ControlWord"].raw = 0x80
            self.node.sdo["ControlWord"].raw = 0x06
            self.node.sdo["ControlWord"].raw = 0x0F
            # Set Go Color to Orange
            self.button_6.SetBackgroundColour(self.orange)

        if quick_test == 1: # Torque
            # Set Mode to Torque (4)
            print("Setting Mode = TORQUE")
            self.node.sdo["SetModeOfOperation"].raw = 4
        elif quick_test == 2: # Velocity
            # Set Mode to Velocity (3)
            print("Setting Mode = VELOCITY")
            self.node.sdo["SetModeOfOperation"].raw = 3
        elif quick_test == 3: # Position
            # Set Mode to Position (1)
            print("Setting Mode = POSITION")
            self.node.sdo["SetModeOfOperation"].raw = 1
            self.node.sdo["ControlWord"].raw = 0x2F # Immediate position mode (not buffered)
            # making Profile Velocity an even RPM to make debugging easier
            # 100 RPM * 4096 cts/sec / 60 sec
            self.node.sdo["ProfileVelocity"].raw = 130000 # cts/s (default)
        elif quick_test == 4: # Homing
            print ("Setting Mode = HOMING")
            self.node.sdo["SetModeOfOperation"].raw = 6
            # set text box value to 0
            self.text_testvalue.SetValue("0")

    def run_test(self, event):  # wxGlade: wxp3_frame.<event_handler>
        #print("Event handler 'run_test'")
        if len(self.network.scanner.nodes) == 0:
            # Error message if no Bus
            print('No active node!')
            msg = ('No active node!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return
        
        if len(self.text_testvalue.GetValue()) == 0: 
            # Error message if no input value
            print('No input value!')
            msg = ('No input value!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return
        
        quick_test = self.choice_test.GetSelection()
        cmd_value = float(self.text_testvalue.GetValue())

        if quick_test == 0:
            # Error Message
            print('No mode selected!')
            msg = ('No mode selected!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()

        elif quick_test == 1: # Torque
            rated_torque = self.node.sdo["RatedTorque"].raw
            if abs(cmd_value / self.gearRatio) > rated_torque:
            # Needs to be based on gear Ratio
                cmd_value = math.copysign(rated_torque * self.gearRatio, cmd_value) # Saturate
            trq_value = round(cmd_value * 1000 / (rated_torque * self.gearRatio)) # Scale
            
            # Needs scaling for accurate gear ratio based torque!!!
            print("Set TargetTorque = {0}".format(cmd_value) + " mNm ({0}".format(round(trq_value/10,2)) + "% max)") # show mNm & percent max
            print("Command CAN value - {}".format(trq_value))
            self.node.sdo["TargetTorque"].raw = trq_value # Send

        elif quick_test == 2: # Velocity
            ctspersec = cmd_value * 4096 / 60 * self.gearRatio
            print("Set Target Velocity = {0}".format(cmd_value) + " RPM")
            # print('Ctspersec: {}'.format(ctspersec))
            # Used to fix old max velocity bug! No longer relevant
            # if ctspersec > self.peak_velocity:
            #     print('Target Velocity Higher than peak motor velocity. Limiting to maximum velocity...')
            #     ctspersec = self.peak_velocity
            #     cmd_value = round(ctspersec / 4096 * 60 / self.gearRatio)
            #     self.text_testvalue.SetValue(str(cmd_value)) 
            # elif ctspersec < -self.peak_velocity:
            #     print('Target Velocity Higher than peak motor velocity. Limiting to maximum velocity...')
            #     ctspersec = -self.peak_velocity
            #     cmd_value = round(ctspersec / 4096 * 60 / self.gearRatio)
            #     self.text_testvalue.SetValue(str(cmd_value)) 
            print("Set TargetVelocity = {0}".format(cmd_value) + " RPM")
            self.node.sdo["TargetVelocity"].raw = ctspersec # Send

        elif quick_test == 3: # Position step
            # On position update
            # Set waypoint entries
            #  607A Target, 6081 Profile Velocity, 6082 Final Velocity, 6083 Accel, 6084 Decel (positive)
            # cmd_value is in degree = 19.1 gear ratio 4096 cts 360 degrees
            ctsvalue = cmd_value / 360 * 4096 * self.gearRatio #* 19.1 # 19.1 for Dev Kit gear ratio 
            print("Set Target Position += {0}".format(cmd_value) + " degrees")
            self.node.sdo["TargetPosition"].raw = self.node.sdo["PositionFeedback"].raw + ctsvalue # Send

            # Wait for StatusWord[12] == 0 (ready to receive new waypoint)
            while self.node.sdo["StatusWord"].raw & 0x1000:
                time.sleep(0.01)
            
            # Set ControlWord to 0x3F (Immediate position, New setpoint)
            self.node.sdo["ControlWord"].raw = 0x3F # Raise new setpoint flag, motor should begin moving

            # Wait for StatusWord[12] == 1 (setpoint acknowledged)
            while not (self.node.sdo["StatusWord"].raw & 0x1000):
                time.sleep(0.01)

            # Set ControlWord to 0x2F (clear new setpoint flag)
            self.node.sdo["ControlWord"].raw = 0x2F

        elif quick_test == 4: # Position step
            self.node.sdo["HomingOffset"].raw = int(cmd_value)
            # start homing
            self.node.sdo["ControlWord"].raw |= 0x0010
            # Wait for StatusWord[12] == 1 (homing attained)
            while not (self.node.sdo["StatusWord"].raw & 0x1000):
                time.sleep(0.1)
            # stop homing
            self.node.sdo["ControlWord"].raw &= ~0x0010

        self.lastMode = quick_test

    def logo_click(self,event): # wxGlade: wxp3_frame.<event_handler>
        #print("Event Handler 'logo_click'")

        if self.ADC_ON == True:
           self.on_off_adc(self)
           self.adcWasON = True
        else:
           self.adcWasON = False

        pageURL = 'https://barrett.com/puck-motor-controller'
        webbrowser.open(pageURL) # Opens Barrett Support Site for marketing / customer help!
        print('Opening support site...')
        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)

    def getMonitor(self):
        try:
            # Read ADC for Puck Temperature, format properly, and update Frame
            ampTemp = self.node.tpdo[3]['Amplifier.Temperature'].raw
            ampTempString = str(ampTemp) + "C"
            if ampTempString != self.PTemp.GetLabel(): # Only updates label if there is a change
                #self.PTemp.SetLabel(ampTempString)
                #Colour Setting
                if ampTemp >= 250:
                    #Also needs buffer
                    if self.PTemp.GetLabel() != 'N/A':
                        print(self.PTemp.GetLabel())
                        self.PTemp.SetForegroundColour(wx.Colour(0,0,0))
                        self.PTemp.SetLabel('N/A')
                elif ampTemp >= self.temp_limit:
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(wx.Colour(245,16,0)) # Red
                elif 50 <= ampTemp < self.temp_limit:
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(self.orange) # Orange
                elif ampTemp < 0:
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(wx.Colour(115,155,208)) # Icy blue
                else:    
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(wx.Colour(0,0,0)) # Black
            if ampTemp >= 100:
                # Turn off test
                #Set Mode to IDLE
                self.lastMode = 0 # Reset lastMode
                self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
                self.button_6.SetBackgroundColour(self.orange)
                self.button_6.SetLabel("Go")
                print("Puck Overheating - Stopping test...")

            # Read ADC for Bus Voltage, format properly, and update Frame
            #currentbyte = self.node.sdo.upload(0x3000,1)
            current = self.node.tpdo[3]['CurrentFeedback'].raw
            current = (current / 1000 * self.i_peak) * 1/math.sqrt(2) / 1000
            currentString = str(round(current,1)) + "A"
            # If current is 0 remove negative sign (if present)
            if round(current,1) == 0 and currentString[0] == "-":
                currentString = currentString[1:]
                
            if currentString != self.VBus.GetLabel():
                self.VBus.SetLabel(currentString)
                #Colour Setting
                if (current <= -self.i_peak / math.sqrt(2) / 1000 * self.peak_factor) or (current >= self.i_peak / math.sqrt(2) / 1000 * self.peak_factor):
                    self.VBus.SetForegroundColour(wx.Colour(245,16,0)) # Red
                elif (current <= -self.i_cont / math.sqrt(2) / 1000) or (current >= self.i_cont / math.sqrt(2) / 1000):
                    self.VBus.SetForegroundColour(self.orange) # Orange
                else:
                    self.VBus.SetForegroundColour(wx.Colour(0,0,0))
            # Read ADC for Motor Temperature, format properly, and update Frame
            #motorTempbyte = self.node.sdo.upload(0x3010,3) # This needs to be the correct value
            motorTemp = self.node.tpdo[3]['Motor.Therm'].raw / 10
            #motorTemp = int.from_bytes(motorTempbyte, byteorder='little', signed='signed')
            motorTempString = str(motorTemp) + "C"
            if True: # adding automatic N/A for Dev Kit App #motorTempString != self.MTemp.GetLabel() and motorTemp != 0 and motorTemp != -8 and motorTemp != -9 and motorTemp < ampTemp + 15:
                #Colour Setting
                if motorTemp == -273:
                    self.MTemp.SetForegroundColour(wx.Colour(0,0,0))
                    motorTempString = 'N/A'
                elif motorTemp >= 100:
                    self.MTemp.SetForegroundColour(wx.Colour(245,16,0))
                elif 75 <= motorTemp < 100:
                    self.MTemp.SetForegroundColour(wx.Colour(255,132,0))
                elif motorTemp < 0:
                    self.MTemp.SetForegroundColour(wx.Colour(115,155,208))
                else:
                    self.MTemp.SetForegroundColour(wx.Colour(0,0,0))
                self.MTemp.SetLabel(motorTempString)
            # elif motorTemp == 0 or motorTemp == -8 or motorTemp == -9 or motorTemp > ampTemp + 15: # Handles case of no motor thermistor present
            #     self.MTemp.SetLabel('N/A')
            #     self.MTemp.SetForegroundColour(wx.Colour(0,0,0))
        except:
            pass

    def getPosition(self): #Get RPM + Update every 10th cycle for 10Hz
        try:
            encPos = self.node.tpdo[4]['PositionFeedback'].raw
            currentSysTime = time.time() # Get Current System time for accurate calc
            
            encPosRad = encPos * 2.0 * math.pi / self.encoderResolution / self.gearRatio # * 0.0015339 / self.gearRatio # added division by gear ratio 

            if self.motorPresent: # and Mode != 0: # Add Mode != 0 to stop updates when in idle (only useful for annoying graphics when no motor attached)
                if abs(encPosRad - self.lastPosRad) > 0.005: # if encPos has changed - this saves CPU usage and limits screen refreshes
                    img = self._dial_base_img.Copy()
                    img._W, img._H = img.GetSize()
                    center = (int(img._W/2),int(img._H/2))
                    img = img.Rotate(encPosRad, center,interpolating=True)
                    self.Dial.SetBitmap(img)
            else:
                self.Dial.SetBitmap(wx.Bitmap(self._dial_base_img))

            #if True: #self.firstRun != True:
               
            PVel = self.node.tpdo[4]['VelocityFeedback'].raw
            RPM = PVel * 60 / 4096 / self.gearRatio 
            RPM = round(RPM / 10, 1)
            RPM = abs(round(RPM *10))
            RPMString = str(RPM)
            self.y = self.y + 1

            # use this to set RPM and position dial off

            #self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE

            if self.y == 10:
                if self.motorPresent != True: # or Mode == 0: # Add Mode == 0 to turn off RPM during idle (only useful for annoying graphics with no motor)
                    self.Vrpm.SetLabel('N/A')
                    self.Vrpm.SetForegroundColour((0,0,0))
                elif RPMString != self.Vrpm.GetLabel():
                    self.Vrpm.SetLabel(RPMString)
                    self.Vrpm.SetForegroundColour((0,0,0))
                self.y = 0
                
            self.lastPosRad = encPosRad #set current position to last
            self.lastSysTime = currentSysTime #set current time to last
            self.firstRun = False
        except:
            #self.network.disconnect()
            print('Lost connection with node ' + str(self.getID()))
            print('Disconnecting...')
            #MyApp.removePuck(self,self.getID())

            self.on_off_adc(self)
            # If ADC Thread is turned off, still need to rescan after disconnect
            self.scan_pucks(None)
            self.on_off_adc(self)

            pass
   
    def onCloseFrame(self,event):
        try:
          self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
          MyApp.removePuck(self,self.getID())
        except:
          pass

        self.Destroy()
        print('Closing Frame...')
        os._exit(0)    

    def on_off_adc(self,event):
        try:
            if len(MyApp.getNodes(self)) > 0:
                # print(event.GetId())
                if self.ADC_ON == False:
                    print('Turning on ADC Monitor...')
                    # Start sync transmission
                    self.network.sync.start(0.01)
                    #Turn on ADC Monitoring
                    self.ADC_ON = True
                    # if event.getId() == '-31989':
                    #     print('yes')
                    #     self.onoff1.SetValue(1)

                elif self.ADC_ON == True:
                    print('Turning off ADC Monitor...')
                    #Turn off ADC Monitoring
                    # Stop sync transmission
                    try:
                        self.network.sync.stop()
                    except:
                        pass
                    self.ADC_ON = False

                    # Reset monitor values to N/A
                    self.VBus.SetLabel('N/A')
                    self.PTemp.SetLabel('N/A')
                    self.MTemp.SetLabel('N/A')
                    self.Vrpm.SetLabel('N/A')
                    self.VBus.SetForegroundColour((0,0,0))
                    self.PTemp.SetForegroundColour((0,0,0))
                    self.MTemp.SetForegroundColour((0,0,0))
                    self.Vrpm.SetForegroundColour((0,0,0))

                    self.Dial.SetBitmap(wx.Bitmap(self._dial_base_img))
                    # if event.getId() == '-31989':
                    #     print('yes')
                    #     self.onoff1.SetValue(0)
                    # self.onoff1.SetValue(0)
            else:
                print('No Puck Connected -')
                print('Turning off ADC Monitor...')
                time.sleep(0.1) # delay for visual effect
                self.onoff1.SetValue(0)
        except:
            # self.on_off_adc(None) # incorrect
            # Reset Button to off
            print('No Puck Connected -')
            print('Turning off ADC Monitor...')
            time.sleep(0.1) # delay for visual effect
            self.onoff1.SetValue(0)
            pass

class MyApp(wx.App):
    def OnInit(self):
        #self.SetTopWindow(self.frame)
        wx.App.ActiveID = []
        wx.App.Nodes = []

        self.frame = MyFrame(None, wx.ID_ANY, "")
        self.frame.Centre()
        self.frame.Show()

        result = self.frame.can_port(None)
        self.Bind(wx.EVT_KEY_DOWN,self.frame.onKeyDown)
        self.Bind(wx.EVT_KEY_UP,self.frame.onKeyUp)

        self.frame.set_tool_tips(None)

        # Transmit an NMT reboot command to this node
        if result == True:
            try:
                print("Booting...")
                self.frame.network.send_message(0x0, [0x81, 0])
                time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
                self.frame.scan_pucks(self)
                self.initialize = self.frame.network.scanner.nodes
                # Placement causes node not to get added!!
                if len(self.getNodes()) == 0:
                    # print('No Pucks active')
                    return True
                # print(self.frame.GetSize())

                self.addPucks(self.frame.getID())
                i = len(self.getNodes())
                if i == 0:
                    return
            except Exception as e:
                # print(e)
                pass

        return True # Added for Windows DEMO - windows can't handle multi bus currently

    def addPucks(self,i): # Adds Puck ID to list of Active Frames
        wx.App.ActiveID.append(i)
        print('Adding Puck...')

    def removePuck(self,i):
        wx.App.ActiveID.remove(i)
        print('Removing Puck...')
        return

    def getPucks(self): # Gets list of IDs in Active Frames
        return wx.App.ActiveID

    def updateNodes(self,i):
        wx.App.Nodes = i

    def getNodes(self):
        return wx.App.Nodes

# ---- Logging ----------------------------------------------------------------

def _setup_logging():
    """Tee stdout/stderr to a timestamped log file. Keeps the 10 most recent logs."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)

    # Rotate: remove oldest logs until fewer than 10 exist (making room for this one)
    existing = sorted(
        f for f in os.listdir(log_dir) if f.startswith('puck_') and f.endswith('.log')
    )
    while len(existing) >= 10:
        os.remove(os.path.join(log_dir, existing.pop(0)))

    timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    log_path = os.path.join(log_dir, f'puck_{timestamp}.log')

    try:
        log_file = open(log_path, 'w', buffering=1)
    except OSError as e:
        print(f"Warning: could not open log file {log_path}: {e}")
        return

    log_file.write(f"=== Puck Utility App ===\n")
    log_file.write(f"Started : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    log_file.write(f"Command : {' '.join(sys.argv)}\n")
    log_file.write(f"{'=' * 23}\n\n")
    log_file.flush()

    class _Tee:
        def __init__(self, original, log):
            self._original = original
            self._log = log
        def write(self, data):
            self._original.write(data)
            self._log.write(data)
        def flush(self):
            self._original.flush()
            self._log.flush()
        def fileno(self):
            return self._original.fileno()
        def isatty(self):
            return self._original.isatty()

    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)

# ---- CLI helpers (no wx dependency) ----------------------------------------

class CLIProgress:
    """Queue-compatible progress sink for CLI use (replaces multiprocessing.Queue)."""
    def put(self, value):
        if isinstance(value, int):
            print(f"\rProgress: {value}%  ", end="", flush=True)
        else:
            print()  # newline after the progress line

def _cli_make_network(can_device):
    network = canopen.Network()
    system = platform.system()
    if system == "Windows":
        network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
    elif system == "Linux":
        network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)
    elif system == "Darwin":
        network.connect(bustype='pcan', channel='PCAN_USBBUS1', bitrate=1000000)
    return network

def _cli_connect(can_device):
    """Connect to CAN bus and scan for nodes. Returns (network, node_ids)."""
    network = _cli_make_network(can_device)
    network.scanner.reset()
    network.scanner.search()
    time.sleep(0.5)
    nodes = list(network.scanner.nodes)
    print(f"Found {len(nodes)} node(s): {nodes}")
    return network, nodes

def _cli_flash(can_device, node_id, fw_path):
    print(f"Flashing node {node_id} with {fw_path}...")
    result = flashp4.flash(can_device, node_id, fw_path, CLIProgress())
    if result:
        print(f"Flash failed: {flashp4.flash_result.get_string[result]}")
        return False
    print("Flash succeeded.")
    time.sleep(0.5)
    return True

def _cli_config(can_device, node_id, csv_path):
    print(f"Uploading config to node {node_id} from {csv_path}...")
    canopen_runner.start(can_device, node_id, 'puck4.eds', csv_path, CLIProgress())
    # Mirror file_to_p3: save all OD entries to EEPROM then reboot
    save_net = _cli_make_network(can_device)
    save_node = save_net.add_node(node_id, 'puck4.eds')
    print("  Saving to EEPROM...")
    default_timeout = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
    canopen.sdo.SdoClient.RESPONSE_TIMEOUT = 1.0
    save_node.sdo['Save']['All'].raw = 0x65766173  # 'save'
    canopen.sdo.SdoClient.RESPONSE_TIMEOUT = default_timeout
    print("  Rebooting puck...")
    save_net.send_message(0x0, [0x81, node_id])
    time.sleep(0.5)
    save_net.disconnect()

def _cli_test_encoder(node):
    print("  Testing encoder stability...")
    node.sdo["SetModeOfOperation"].raw = 0
    time.sleep(1)
    t_end = time.time() + 1
    readings = []
    while time.time() < t_end:
        readings.append(node.sdo['PositionFeedback'].raw)
    variation = max(readings) - min(readings)
    max_allowed = 8
    print(f"  Encoder variation: {variation} counts (max {max_allowed})")
    if variation > max_allowed:
        print(f"  WARNING: Encoder readings unstable! "
              f"variation={variation}, max acceptable={max_allowed}")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True

def _cli_calibrate_ibias(node):
    print("  Calibrating current sense bias (ibias)...")
    node.sdo["ControlWord"].raw = 0x80
    node.sdo["ControlWord"].raw = 0x06
    node.sdo["ControlWord"].raw = 0x0F
    node.sdo['Theta_e'].raw = 0x7FFF
    node.sdo['Motor']['ud'].raw = 0
    node.sdo["SetModeOfOperation"].raw = 12
    time.sleep(1)
    for ch in ['Alpha', 'Beta']:
        print(f"  Previous {ch} bias = {node.sdo[ch]['Bias'].raw}")
        filt = node.sdo[ch]['Filtered'].raw
        filt = (filt >> 4) + ((filt & 0x0008) >> 3)
        node.sdo[ch]['Bias'].raw = filt
        print(f"  New {ch} bias = {filt}")
    node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x03)
    node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x03)
    a_bias = node.sdo['Alpha']['Bias'].raw
    b_bias = node.sdo['Beta']['Bias'].raw
    node.sdo["SetModeOfOperation"].raw = 0
    error = 0.5
    lo, hi = round(2048 * (1 - error)), round(2048 * (1 + error))
    if a_bias > hi or a_bias < lo or b_bias > hi or b_bias < lo:
        print(f"  WARNING: iSense bias out of bounds! "
              f"Alpha={a_bias}, Beta={b_bias}, acceptable range {lo}-{hi}")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True

def _cli_calibrate_igainfactor(node):
    print("  Calibrating current sense gain factor (igainfactor)...")
    node.sdo['Alpha']['Gainfactor'].raw = 4096
    node.sdo['Beta']['Gainfactor'].raw = 4096
    node.sdo["ControlWord"].raw = 0x80
    node.sdo["ControlWord"].raw = 0x06
    node.sdo["ControlWord"].raw = 0x0F
    node.sdo["SetModeOfOperation"].raw = 12
    node.sdo['Theta_e'].raw = 0x7FFF
    cal_current = node.sdo['Calibration']['i_cal'].raw
    i_peak = node.sdo['Calibration']['i_peak'].raw
    if cal_current > i_peak:
        cal_current = i_peak
    time.sleep(1)
    motor_ud = 0
    motor_id = node.sdo['Motor']['id'].raw
    while (motor_id < 1000 and node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < cal_current and motor_ud < 32000:
        motor_ud += 100
        node.sdo['Motor']['ud'].raw = motor_ud
        time.sleep(0.05)
    time.sleep(1)
    a_filt = node.sdo['Alpha']['Filtered'].raw
    a_filt = (a_filt >> 4) + ((a_filt & 0x0008) >> 3)
    node.sdo['Theta_e'].raw = -0x4000
    time.sleep(1)
    b_filt = node.sdo['Beta']['Filtered'].raw
    b_filt = (b_filt >> 4) + ((b_filt & 0x0008) >> 3)
    node.sdo["SetModeOfOperation"].raw = 0
    abias = node.sdo['Alpha']['Bias'].raw
    bbias = node.sdo['Beta']['Bias'].raw
    gf_raw = 4096 * (a_filt - abias) / (b_filt - bbias)
    node.sdo['Beta']['Gainfactor'].raw = gf_raw
    gainfactor = round(gf_raw)
    print(f"  New Beta Gainfactor = {gainfactor}")
    node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x06)
    node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x06)
    error = 0.10
    lo, hi = round(4096 * (1 - error)), round(4096 * (1 + error))
    if gainfactor > hi or gainfactor < lo:
        print(f"  WARNING: Beta Gainfactor out of bounds! "
              f"{gainfactor}, acceptable range {lo}-{hi}")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True

def _cli_calibrate_enczero(node):
    print("  Calibrating encoder zero...")
    node.sdo["ControlWord"].raw = 0x80
    node.sdo["ControlWord"].raw = 0x06
    node.sdo["ControlWord"].raw = 0x0F
    node.sdo["SetModeOfOperation"].raw = 12
    node.sdo['Theta_e'].raw = -0x1000
    cal_current = node.sdo['Calibration']['i_cal'].raw
    i_peak = node.sdo['Calibration']['i_peak'].raw
    if cal_current > i_peak:
        cal_current = i_peak
    motor_ud = 0
    motor_id = node.sdo['Motor']['id'].raw
    while (motor_id < 1000 and node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < cal_current and motor_ud < 32000:
        motor_ud += 100
        node.sdo['Motor']['ud'].raw = motor_ud
        time.sleep(0.05)
    pos0 = node.sdo['Encoder']['RawPosition'].raw
    startPos1 = node.sdo['PositionFeedback'].raw
    for i in range(int(-0x1000), 0, int(0x1000 / 32)):
        node.sdo['Theta_e'].raw = i
        time.sleep(0.05)
    time.sleep(0.25)
    pos1 = node.sdo['Encoder']['RawPosition'].raw
    zeroPos1 = node.sdo['PositionFeedback'].raw
    node.sdo['Theta_e'].raw = 0x1000
    time.sleep(1)
    startPos2 = node.sdo['PositionFeedback'].raw
    for i in range(int(0x1000), 0, int(-0x1000 / 32)):
        node.sdo['Theta_e'].raw = i
        time.sleep(0.05)
    time.sleep(0.25)
    pos2 = node.sdo['Encoder']['RawPosition'].raw
    zeroPos2 = node.sdo['PositionFeedback'].raw
    enc_res = node.sdo['EncoderConfig']['Resolution'].raw
    poles = node.sdo['Calibration']['poles'].raw
    cts_per_elec = enc_res * 2 / poles
    if abs(pos1 - pos2) > enc_res / 2:
        if pos1 > pos2:
            pos1 += enc_res
        else:
            pos2 += enc_res
    pos = int(((pos1 + pos2) / 2) % cts_per_elec)
    if abs(pos1 - pos0) < cts_per_elec / 2:
        e_polarity = math.copysign(1, pos1 - pos0)
    else:
        e_polarity = -math.copysign(1, pos1 - pos0)
    node.sdo['Calibration']['e_polarity'].raw = e_polarity
    node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x02)
    print(f"  Electrical polarity = {e_polarity}")
    print(f"  Previous e_zero = {node.sdo['Calibration']['e_zero'].raw}, new e_zero = {pos}")
    node.sdo['Calibration']['e_zero'].raw = pos
    node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x01)
    pos_change1 = round(abs(startPos1 - zeroPos1) * (360 / 4096) * poles)
    pos_change2 = round(abs(startPos2 - zeroPos2) * (360 / 4096) * poles)
    node.sdo["SetModeOfOperation"].raw = 0
    error = 0.25
    min_jump = round(22.5 * (1 - error))
    if pos_change1 < min_jump or pos_change2 < min_jump:
        cal_torque = cal_current * node.sdo['Calibration']['kt'].raw / 1000
        print(f"  WARNING: Encoder zero failed! "
              f"Jump1={pos_change1}°, Jump2={pos_change2}°, expected >={min_jump}°, "
              f"cal torque={cal_torque}mNm")
        resp = input("  Continue calibration? [y/n]: ").strip().lower()
        return resp == 'y'
    return True

def _cli_calibrate_all(node):
    print("  Running full calibration sequence...")
    if not _cli_test_encoder(node):
        print("  Calibration aborted.")
        return False
    if not _cli_calibrate_ibias(node):
        print("  Calibration aborted.")
        return False
    if not _cli_calibrate_igainfactor(node):
        print("  Calibration aborted.")
        return False
    if _cli_calibrate_enczero(node) is False:
        print("  Calibration aborted.")
        return False
    print("  Calibration complete!")
    return True

def _ini_path(value):
    """Strip optional surrounding quotes from an INI path value."""
    if value and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value

def _cli_system_config(can_device, ini_path):
    cfg = configparser.ConfigParser()
    cfg.read(ini_path)

    network, found_ids = _cli_connect(can_device)
    network.disconnect()

    configured_ids = []
    for section in cfg.sections():
        node_id = int(cfg[section]['ID'])
        csv_path = _ini_path(cfg[section]['CSV'])
        fw_version = cfg[section].get('fw_version')
        fw_path = _ini_path(cfg[section].get('fw'))
        if node_id not in found_ids:
            print(f"Node {node_id} ({section}) not found on bus, skipping.")
            continue
        print(f"\n--- Configuring node {node_id} ({section}) ---")
        if fw_version and fw_path:
            ver_net = _cli_make_network(can_device)
            ver_node = ver_net.add_node(node_id, 'puck4.eds')
            version = get_version(ver_node.sdo['MfgSoftwareVersion'].raw)
            ver_net.disconnect()
            if version != fw_version:
                print(f"  Firmware {version} → updating to {fw_version}...")
                time.sleep(0.2)
                if not _cli_flash(can_device, node_id, fw_path):
                    print(f"  Skipping config for node {node_id} due to flash failure.")
                    continue
            else:
                print(f"  Firmware {version} up to date.")
        _cli_config(can_device, node_id, csv_path)
        configured_ids.append(node_id)

    if configured_ids:
        resp = input("\nCalibration required after configuration. Calibrate now? [y/n]: ").strip().lower()
        if resp == 'y':
            for node_id in configured_ids:
                print(f"\n--- Calibrating node {node_id} ---")
                cal_net = _cli_make_network(can_device)
                cal_node = cal_net.add_node(node_id, 'puck4.eds')
                _cli_calibrate_all(cal_node)
                cal_net.disconnect()

# ---- Entry point ------------------------------------------------------------

if __name__ == "__main__":
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog='puckutilityapp.py',
        description='Puck Utility App — launches GUI when run with no arguments, '
                    'or runs headlessly when CLI flags are provided.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Must be run from the puckutility directory so that puck4.eds is accessible.

Examples:
  # Launch GUI
  python3 puckutilityapp.py

  # Flash firmware to node 1
  python3 puckutilityapp.py --can can0 --id 1 --flash firmware/P4-v1.1.5.bin

  # Upload config CSV to nodes 1 and 2
  python3 puckutilityapp.py --can can0 --id 1 2 --config config/motor.csv

  # Calibrate all discovered pucks
  python3 puckutilityapp.py --can can0 --all --calibrate

  # Apply system config INI (handles firmware check, config upload, optional calibration)
  python3 puckutilityapp.py --can can0 --system-config system.ini
"""
    )
    parser.add_argument('--can', metavar='DEVICE',
                        help='CAN device (e.g. can0 on Linux, 0 for PCAN_USBBUS1 on Windows)')
    parser.add_argument('--id', type=int, nargs='+', metavar='ID',
                        help='One or more target node IDs')
    parser.add_argument('--all', action='store_true',
                        help='Scan the bus and apply operation to all discovered pucks')

    ops = parser.add_mutually_exclusive_group()
    ops.add_argument('--scan', action='store_true',
                     help='Scan the CAN bus and print all discovered node IDs')
    ops.add_argument('--flash', metavar='FIRMWARE',
                     help='Path to firmware file (.bin or .ebin)')
    ops.add_argument('--config', metavar='CSV',
                     help='Path to motor configuration CSV file')
    ops.add_argument('--calibrate', action='store_true',
                     help='Run full calibration (test_encoder, ibias, igainfactor, enczero)')
    ops.add_argument('--system-config', metavar='INI', dest='system_config',
                     help='Path to system configuration INI file')

    args = parser.parse_args()

    # No CLI args → launch GUI
    if len(sys.argv) == 1:
        app = MyApp(0)
        app.MainLoop()
        sys.exit(0)

    # All operations require --can
    if not args.can:
        parser.error('--can is required')

    # --scan: list nodes on the bus and exit
    if args.scan:
        net, found = _cli_connect(args.can)
        net.disconnect()
        sys.exit(0)

    # --system-config is self-contained; --id/--all are not used with it
    if args.system_config:
        _cli_system_config(args.can, args.system_config)
        sys.exit(0)

    # Remaining operations need an explicit target
    if not args.id and not args.all:
        parser.error('specify target nodes with --id or use --all to scan')
    if not (args.flash or args.config or args.calibrate):
        parser.error('specify an operation: --scan, --flash, --config, --calibrate, or --system-config')

    # Always scan first so we can validate requested IDs against the live bus
    scan_net, found_ids = _cli_connect(args.can)
    scan_net.disconnect()
    if not found_ids:
        print("No nodes found on bus.")
        sys.exit(1)

    if args.all:
        node_ids = found_ids
    else:
        node_ids = []
        for nid in args.id:
            if nid in found_ids:
                node_ids.append(nid)
            else:
                print(f"Warning: node {nid} not found on bus, skipping.")
        if not node_ids:
            print("None of the requested nodes are present on the bus.")
            sys.exit(1)

    # Execute operation across all validated target nodes
    if args.calibrate:
        cal_net = _cli_make_network(args.can)
    for node_id in node_ids:
        print(f"\n--- Node {node_id} ---")
        if args.flash:
            _cli_flash(args.can, node_id, args.flash)
        elif args.config:
            _cli_config(args.can, node_id, args.config)
        elif args.calibrate:
            cal_node = cal_net.add_node(node_id, 'puck4.eds')
            _cli_calibrate_all(cal_node)
    if args.calibrate:
        cal_net.disconnect()
