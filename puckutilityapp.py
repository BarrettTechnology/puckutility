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
#import gettext
from puckutilityapp_gui import puckutilityapp_frame
from calibrate_menu import calibrate
from factory_menu import factory
import OnOffButton

import canopen_runner

import os
import canopen
import platform
import time
import subprocess
import math
import semver
from threading import Thread
import time
import webbrowser
import sys
import math
import datetime
import canopen_runner
import click
import threading
import wx.lib.agw.pygauge as PG

# import pyserial
# import slcan

# TODO
# Possibly add a way to update all puck firmware??
# Look into possible issues with Pucks responding to sync messages when not in focus (this appears to be caused by COB ID only being updated when configuration is set)
# If connection is lost, something needs to reset the on/off *** This is very annoying
# SHOULD use RPDOs to handle control mode in the future and command values! This is the correct way to handle (needs an issue and addition for v1.1.5)
# Do not clear tpdo 1 and 2, use these in the monitor / position
# refresh looks awful on windows

# WISH LIST:
# Add loading for firmware to status bar

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
        # print('you dropped it!!!!!')
        for filepath in filenames:
            self.window.ProcessDroppedFile(filepath)
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
        icons = wx.Icon("images/BarrettLogo.png")
        self.SetBackgroundColour(wx.Colour(255,255,255))
        USE_BUFFERED_DC = True

        # Init drop target
        dt = DropTarget(self)
        self.SetDropTarget(dt)

        # self.LAUNCH = '2F.11.34.01.04.00.00.00'

        # Initialize self variables
        # self.gearRatio = 1
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

        # Barrett colors
        self.blue = '#253B92'
        self.orange = '#FF7C1B'
        self.gray = '#8C8C8C'

        self.peak_factor = 0.75 # % Peak for Current Colors

        # Setup Window + Icon
        self.SetIcon(wx.Icon('images/BarrettIcon.png'))
        self.SetTitle("Puck Utility App - v1.1.5")
        self.button_6.SetBackgroundColour(self.gray) # Initialize with gray button in idle
        self.Bind(wx.EVT_KEY_DOWN,self.onKeyDown)
        self.Bind(wx.EVT_CLOSE, self.onCloseFrame)
        self.backgroundBMP = wx.Bitmap("images/Background.png") # recreating the BMP each rewrite causes massive lagging this is much better!
        # Bind backgound function to assign bitmap
        self.Bind(wx.EVT_ERASE_BACKGROUND, self.OnEraseBackground)

        # Add button to onoffpanel
        # MAY WANT TO INCREASE THE SIZE OF THIS IN CASE IT GIVES BETTER RESOLUTION
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.onoff1 = OnOffButton.OnOffButton(self.onoffpanel, -1, size=(50, 34), initial=0, border=False, name="2")
        self.onoff1.Bind(OnOffButton.EVT_ON_OFF, self.on_off_adc)
        # Demonstrate individual control adjustments
        self.onoff1.SetOnColour(self.orange) # Barrett Orange
        self.onoff1.SetOnForegroundColour(self.gray) # Barrett Blue
        self.onoff1.SetOffColour(self.blue) # Barret Gray
        self.onoff1.SetOffForegroundColour(self.gray) # Barrett Blue
        self.onoff1.SetToolTip("ADC Monitor ON/OFF")
        sizer.Add(self.onoff1, 0, wx.ALIGN_CENTER)
        self.onoffpanel.SetSizer(sizer)

        # Disable the unimplemented menu items
        menu = "Calibrate"
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
        self.progress.SetBarColour(self.orange)
        self.progress.SetBarGradient(('#FFFFFF',self.orange))
        # print(self.progress.GetBarGradient())
        # self.progress.SetBarGradient()
        self.progress.Hide()
        # self.progress.SetBorderColor(wx.BLACK)
        self.dc = wx.ScreenDC()
        # Initial Positioniing
        self.RepositionGauge()

    
    #this may be unnecessary
    # def OnResize(self,event):
    #     self.RepositionGauge()
    #     event.Skip()

    def RepositionGauge(self):
        rect = self.frame_statusbar.GetFieldRect(1)
        # Get text width and add this to the start spot!!
        text = "Progress: 100%"
        width, height = self.dc.GetTextExtent(text)
        # print(width)
        # self.progress.SetBorderPadding(25)
        self.progress.SetPosition((rect.x + 20 + width, int(rect.y * 2 + 2)))
        # self.progress.
        self.progress.SetSize((rect.width - 6, rect.height - 8))

    def UpdateProgress(self,value):
        self.progress.SetValue(value)

    def OnStartTask(self,event):
        self.thread = threading.Thread(target=self.WorkerThread)
        self.thread.daemon = True
        self.thread.start()
        self.progress.Show()

    def WorkerThread(self):
        for i in range (101):
            time.sleep(0.02)
            wx.CallAfter(self.UpdateUI,i)
        time.sleep(1)
        # i = 0
        # wx.CallAfter(self.UpdateUI,i)
        wx.CallAfter(self.OnTaskComplete)

    def OnTaskComplete(self):
        self.progress.Hide()
        self.frame_statusbar.SetStatusText("Ready", 1)

    def UpdateUI(self,value):
        self.progress.SetValue(value)
        self.frame_statusbar.SetStatusText(f"Progress: {value}%",1)

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
        else:
            print('Invalid file...')
            # add a popup
            msg = 'Invalid file type! \n\n\nExpected Extensions-\nFirmware: ".ebin"\nConfig: ".csv"'
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

    def onKeyDown(self,event):
        event.Skip()
        # print(event.GetKeyCode())
        if event.GetKeyCode() == 27: # ESC
            self.onCloseFrame(None)
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
        # print('Numerator: {}'.format(motor_rev))
        # print('Denominator: {}'.format(shaft_rev))
        self.gearRatio = motor_rev / shaft_rev

        self.i_cont = self.node.sdo.upload(0x3011,8)
        self.i_cont = int.from_bytes(self.i_cont, byteorder='little',signed=False)
        print('I_cont: {}'.format(self.i_cont))
        self.i_peak = self.node.sdo.upload(0x3011,9)
        self.i_peak = int.from_bytes(self.i_peak, byteorder='little',signed=False)
        # print('I_peak: {}'.format(self.i_peak))

        self.temp_limit = self.node.sdo.upload(0x2384,9)
        self.temp_limit = int.from_bytes(self.temp_limit, byteorder='little',signed=False)

        self.temp_limited_current = self.node.sdo.upload(0x3025,3)
        self.temp_limited_current = int.from_bytes(self.temp_limited_current, byteorder='little',signed=False)
        # print('I_temp_limited: {}'.format(self.temp_limited_current))

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

    def Rescan(self):
        print('Out of Date - rescanning!')
        self.scan_pucks(None)
        self.Rescanning = False

    def can_port(self,event):
        #print("Event handler 'can_port'")
        if(self.ADC_ON == True):
            self.on_off_adc(self) # Turn off adc 
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
          #print('network reset')
          self.network.scanner.search()
          #print('search completed')
        except Exception as e: 
            print(e)
            print('No CAN driver found!')
            msg = 'No CAN bus found! \nCheck connection and try again'
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            # Try to clear out selection of select ID and set ID
            n = ''
            self.choice_id.SetItems([n])
            self.text_id.ChangeValue(str(n))

            dlg.Destroy()
            return
        # We may need to wait a short while here to allow all nodes to respond
        time.sleep(0.05)
        #self.scan_pucks(None)


    def scan_pucks(self, event):  # wxGlade: wxp3_frame.<event_handler>
        #print("Event handler 'scan_pucks'")
        #print(str(datetime.datetime.now()) + " Event handler 'scan_pucks'")
        # Set Mode to IDLE in case test is active

        self.frame_statusbar.SetStatusText("Scanning Pucks...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        self.OnStartTask(None)

        if self.lastMode != 0:
            self.lastMode = 0 # Reset lastMode
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
            self.button_6.SetBackgroundColour(self.gray)
            self.button_6.SetLabel("Go")
            print("Idling...")
        
        # click.echo("Searching for nodes...")
        # with click.progressbar(search_ids, length=len(search_ids)) as search_bar:
        #     for node_id in search_bar:
        #         try:
        #             utils.can_sdo_upload(node_id, 0x1000, 0x0000, timeout=.01)
        #             found_ids.append(node_id)
        #         except Exception as e:
        #             logger.debug(e)
        # if found_ids:
        #     click.echo("Found: {}".format(found_ids))
        # else:
        #     click.echo("Found none")
        #     raise SystemExit(1)
        # might also be such a thing as gauge?
        
        # self.progress_bar = wx.Gauge(self.frame_statusbar, -1, style=wx.GA_HORIZONTAL|wx.GA_SMOOTH)

        try:
            # Think we need these  for scan to work...
            # This will attempt to read an SDO from nodes 1 - 127
            self.network.scanner.reset()
            #print('network reset')
            self.network.scanner.search()
            #print('search completed')
            time.sleep(0.5)

            for node_id in self.network.scanner.nodes:
                print("Found node %d!" % node_id) 

            MyApp.updateNodes(self, self.network.scanner.nodes)

            # Populate the node choice list
            self.choice_id.SetItems([str(i) for i in self.network.scanner.nodes])

            if self.init:                   
                self.initialize = self.network.scanner.nodes              
                print('Initializing CAN bus...')
                if len(self.initialize) > 0:
                    self.init = False
                    print('Success!')

            # If we found at least one, select the first
            if len(self.network.scanner.nodes) > 0:
                if self.getID() == 0:
                    self.choice_id.SetSelection(self.getID()) # This is actually what sets the initial
                else:
                    # do some rescan if not in self scanner (THIS IS WHERE THE NOT IN LIST BUG OCCURS)
                    indexID = self.network.scanner.nodes.index(self.getID())
                    self.choice_id.SetSelection(indexID)
                self.select_id(None)

            else:
                if(node_id == 127):
                    return
                print('No Pucks Found') # Establish error for no pucks
                msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                dlg = wx.MessageDialog(None,msg)
                dlg.ShowModal()
                dlg.Destroy()
                return
            #print(str(datetime.datetime.now()) + " Complete!!!")
        except Exception as e: 
            try:
                if(node_id == 127):
                    return
            except:
                print('No CAN driver found!')
                msg = 'No CAN bus found! \nCheck connection and try again'
                dlg = wx.MessageDialog(None,msg)
                dlg.ShowModal()
                dlg.Destroy()
                return
            
        self.frame_statusbar.SetStatusText("Ready", 1)
        self.frame_statusbar.Update()
        wx.Yield()

    def select_id(self, event):  # wxGlade: wxp3_frame.<event_handler>
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
        self.configure_Puck() # This makes sure all pucks are configured to remove bug with first round adc on turning puck idle

    def set_id(self, event):  # wxGlade: wxp3_frame.<event_handler>
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
        if int(self.text_id.GetValue()) > 127 or int(self.text_id.GetValue()) < 1:
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

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

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

        self.network.send_message(int(node_id), [0x5A, 0xA6]) # Ping command
        time.sleep(0.2) # Wait for reboot
        try:
            version = get_version(self.node.sdo['MfgSoftwareVersion'].raw)
        except:
            version = get_version(1 << 24) # Assume version 1.0.0

        print("Found bootloader version: {0}".format(version))

        if semver.match(version, '==1.0.0') and platform.system() != "Windows":
            msg = "To update firmware, please run this program under Windows."
            print(msg)
            wx.MessageBox(msg, 'Info', wx.OK | wx.ICON_INFORMATION)
            return
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
                print("Rebooting puck")
                self.network.send_message(0x0, [0x81, int(node_id)])
                time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
                # self.network.send_message(0x4, [self.LAUNCH, int(node_id)])
                self.configure_Puck()
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
        self.network.disconnect()

        if semver.match(version, '==1.0.0'):
            l = ['blhost', '-p', can_device + "," + node_id, 'flash-erase-all']
            subprocess.call(l) # Note: this waits until the subprocess exits

            l = ['blhost', '-p', can_device + "," + node_id, 'write-memory', '0x8000', pathname]
            subprocess.call(l) # Note: this waits until the subprocess exits

            # blhost -p can0,1 reset
            # blhost -p can0,1 execute 0 0 0 (address, arg, stack)
            l = ['blhost', '-p', can_device + "," + node_id, 'reset']
            subprocess.call(l) # Note: this waits until the subprocess exits
        else:
            if platform.system() == "Windows":
                python_name = "python"
            else:
                python_name = "python3"
            l = [python_name, "flashp4.py", can_device, node_id, pathname]
            
            subprocess.call(l) # Note: this waits until the subprocess exits

        # Re-scan
        self.can_port(None)
        self.scan_pucks(None)
        # timeFinish = round(time.time() - timeStart,2)
        # print('Time elapsed: {}'.format(timeFinish))
        #print("Establishing a new network...")
        #self.network = canopen.Network()
        #
        #if platform.system() == "Windows":
        #  self.network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
        #elif platform.system() == "Linux":
        #  self.network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)
        #self.node = self.network.add_node(int(node_id), 'puck3.eds')
        time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
        #self.configure_Puck()
        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)

    def file_to_p3(self, event, path=False):  # wxGlade: wxp3_frame.<event_handler>
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
            print('no path')
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

        self.frame_statusbar.SetStatusText("Updating configuration...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

        print("Writing OD entries")
        self.network.disconnect()
        
        success = canopen_runner.start(can_device, int(node_id),'puck4.eds', pathname)
        
        if success == True:
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
            if ctspersec > self.peak_velocity:
                print('Target Velocity Higher than peak motor velocity. Limiting to maximum velocity...')
                ctspersec = self.peak_velocity
                cmd_value = round(ctspersec / 4096 * 60 / self.gearRatio)
                self.text_testvalue.SetValue(str(cmd_value)) 
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
            #ampTempbyte = self.node.sdo.upload(0x3000,2)
            ampTemp = self.node.tpdo[3]['Amplifier.Temperature'].raw
            #ampTemp = int.from_bytes(ampTempbyte, byteorder='little', signed='signed')
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
                
            # i2t_value = self.node.tpdo[2]['i2t.Value'].raw
            # print(i2t_value)
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
                self.MTemp.SetLabel(motorTempString)
                #Colour Setting
                if motorTemp >= 100:
                    self.MTemp.SetForegroundColour(wx.Colour(245,16,0))
                elif 75 <= motorTemp < 100:
                    self.MTemp.SetForegroundColour(wx.Colour(255,132,0))
                elif motorTemp < 0:
                    self.MTemp.SetForegroundColour(wx.Colour(115,155,208))
                else:
                    self.MTemp.SetForegroundColour(wx.Colour(0,0,0))
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
                    img = wx.Image('images/dialnobgcroppedscaled.png')
                    img._W, img._H = img.GetSize()
                    center = (int(img._W/2),int(img._H/2))
                    img = img.Rotate(encPosRad, center,interpolating=True)
                    self.Dial.SetBitmap(img)
            else:
                img = wx.Image('images/dialnobgcroppedscaled.png')
                self.Dial.SetBitmap(img)

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
        if self.ADC_ON == False:
            print('Turning on ADC Monitor')
            # Start sync transmission
            self.network.sync.start(0.01)
            #Turn on ADC Monitoring
            self.ADC_ON = True
            # # Change image to -
            # negativeBitmap = wx.Bitmap('images/negative-.png')
            # self.Plus.SetBitmap(negativeBitmap)
        elif self.ADC_ON == True:
            print('Turning off ADC Monitor')
            #Turn off ADC Monitoring
            # Stop sync transmission
            self.network.sync.stop()
            self.ADC_ON = False
            # # Change image to +
            # positiveBitmap = wx.Bitmap('images/plus+.png')
            # self.Plus.SetBitmap(positiveBitmap) 
            # Reset monitor values to N/A
            self.VBus.SetLabel('N/A')
            self.PTemp.SetLabel('N/A')
            self.MTemp.SetLabel('N/A')
            self.Vrpm.SetLabel('N/A')
            self.VBus.SetForegroundColour((0,0,0))
            self.PTemp.SetForegroundColour((0,0,0))
            self.MTemp.SetForegroundColour((0,0,0))
            self.Vrpm.SetForegroundColour((0,0,0))

            img = wx.Image('images/dialnobgcroppedscaled.png')
            self.Dial.SetBitmap(img)

class MyApp(wx.App):
    def OnInit(self):
        #self.SetTopWindow(self.frame)
        wx.App.ActiveID = []
        wx.App.Nodes = []

        # Setup CAN network
        # TODO BUG Now you can't switch CAN ports!!! need this as a function that can be called?
        # Is this still true?

        # With new firmware and no configuration, adc bugs out big time if it tries to turn on

        self.frame = MyFrame(None, wx.ID_ANY, "")
        self.frame.Centre()
        self.frame.Show()
        # can make this into a try, and set to reconnect on state button?
        self.frame.can_port(None)
        self.Bind(wx.EVT_KEY_DOWN,self.frame.onKeyDown)
        # Maybe set this ^ on a while loop for when no bus is active
        # Transmit an NMT reboot command to this node
        print("Booting...")
        self.frame.network.send_message(0x0, [0x81, 0])
        time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
        # self.frame.network.send_message(0x4, [self.LAUNCH, 127])
        #self.frame.network = self.network
        self.frame.scan_pucks(self)
        self.initialize = self.frame.network.scanner.nodes
        # Placement causes node not to get added!!
        if len(self.getNodes()) == 0:
            print('No Pucks active')
            return True
        # print(self.frame.GetSize())

        self.addPucks(self.frame.getID())
        i = len(self.getNodes())
        if i == 0:
            return
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

if __name__ == "__main__":
    #gettext.install("app") # replace with the appropriate catalog name
    app = MyApp(0)
    app.MainLoop()
