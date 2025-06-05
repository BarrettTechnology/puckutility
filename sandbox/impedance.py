#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import math
import canopen
import socket
from timeit import default_timer as timer
import wx
import struct
import platform
from onoffbutton import OnOffButton, EVT_ON_OFF  # Import the custom OnOffButton control

# Teleplot configuration
teleplotAddr = ("127.0.0.1", 47269)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Global variables for impedance parameters
target_position = 0
stiffness = 100
target_velocity = 0
damping = 10
target_torque = 0
command_type = "None"  # Default command type
play = False  # Play/Pause state
amplitude = 4096  # Default amplitude
period = 5  # Default period
lastUpdate = 0



def OpEnable():
    # Clear faults, RTSO, OpEnabled
    print("Going OpEnabled")
    node.sdo["ControlWord"].raw = 0x80
    node.sdo["ControlWord"].raw = 0x06
    node.sdo["ControlWord"].raw = 0x0F

def home():
    global node
    print("Setting Mode = Homing")
    node.sdo["ControlWord"].raw = 0x0F # Clear any mode-specific bits
    node.sdo["SetModeOfOperation"].raw = 6
    node.sdo["HomingOffset"].raw = 0 # Initialize position to zero
    node.sdo["HomingMethod"].raw = 37 # Home immediate, no limit switch
    node.sdo["ControlWord"].raw = 0x1F # Start homing
    while not (node.sdo["StatusWord"].raw & 0x1000): # Wait for homing complete
        time.sleep(0.1)
    node.sdo["ControlWord"].raw = 0x0F # Clear homing flag

def FloatToU32(f):
    """
    Convert a float to a 32-bit unsigned integer.
    This is used for SDO downloads of floating-point values.
    """
    return struct.unpack('I', struct.pack('f', f))[0]

def U32ToFloat(u):
    """
    Convert a 32-bit unsigned integer to a float.
    This is used for SDO uploads of floating-point values.
    """
    return struct.unpack('f', struct.pack('I', u))[0]

def configure_impedance_mode():
    global node
    rate = 100  # Hz

    # Read PDO configuration from the actuator
    node.tpdo.read()
    node.rpdo.read()

    # Initialize impedance parameters
    node.rpdo[2]["TargetPosition"].raw = target_position
    node.rpdo[2]["TargetVelocity"].raw = target_velocity
    node.rpdo[1]["TargetTorque"].raw = target_torque

    node.sdo["ImpCtrl"]["Stiffness"].raw = FloatToU32(stiffness)
    node.sdo["ImpCtrl"]["Damping"].raw = FloatToU32(damping)
    
    # Each time we receive this PDO from the puck, execute a callback
    node.tpdo[1].add_callback(tpdo1_callback)
    node.tpdo[2].add_callback(tpdo2_callback)

    # Disable Heartbeats
    node.sdo["HeartbeatPeriod"].raw = 0

    # Set up the Cyclic Sync timing (only used in Cyclic Sync modes)
    node.sdo["Cyclic"]["InterpolationPeriod"].raw = 1000 / rate # milliseconds
    node.sdo["Cyclic"]["InterpolationScale"].raw = -3 # milliseconds

    OpEnable()

    # Set RPDO ControlWord to 0x0F (active)
    node.rpdo[1]["ControlWord"].raw = 0x0F

    #print("Setting Mode = Impedance Control")
    #node.rpdo[1]["SetModeOfOperation"].raw = 13  # Impedance Control Mode

def generate_command(elapsed, period, amplitude):
    """
    Generate commanded position and velocity based on the selected command type.
    """
    global target_position, target_velocity

    if command_type == "Sinusoidal":
        position = int(amplitude * math.sin(2 * math.pi * elapsed / period))
        velocity = int((2 * math.pi * amplitude / period) * math.cos(2 * math.pi * elapsed / period))
    elif command_type == "Square":
        position = int(amplitude if (elapsed % period) < (period / 2) else -amplitude)
        velocity = 0  # Square wave has instantaneous velocity changes
    elif command_type == "Triangular":
        phase = (elapsed % period) / period
        if phase < 0.5:
            position = int(amplitude * (4 * phase - 1))
            velocity = int(4 * amplitude / period)
        else:
            position = int(amplitude * (3 - 4 * phase))
            velocity = int(-4 * amplitude / period)
    else:
        position = target_position
        velocity = target_velocity

    return position, velocity

def tpdo1_callback(msg):
    global node
    
    # Store data
    status = node.tpdo[1]['StatusWord'].raw
    mode = node.tpdo[1]['ReadModeOfOperation'].raw
    pos = node.tpdo[1]['PositionFeedback'].raw

    sendTelemetry("PositionFeedback", pos)

def tpdo2_callback(msg):
    global node
    global start
    global target_torque
    global imp
    global lastUpdate

    maxtrq = 1000     # /1000 of rated torque
    maxvel = 20000    # cts/sec
    maxpos = 5 * 4096 # 5 revolutions
    
    # Store data
    vel = node.tpdo[2]['VelocityFeedback'].raw
    current = node.tpdo[2]['CurrentFeedback'].raw

    sendTelemetry("VelocityFeedback", vel)
    sendTelemetry("CurrentFeedback", current)

    # Calculate new pos/vel commands based on wave type
    global amplitude, period
    
    elapsed = timer() - start
    position, velocity = generate_command(elapsed, period, amplitude)
    node.rpdo[2]["TargetPosition"].raw = position
    node.rpdo[2]["TargetVelocity"].raw = velocity
    node.rpdo[1]["TargetTorque"].raw = target_torque

    if int(elapsed * 10) % 10 != int(lastUpdate * 10) % 10:
        imp.entry_position.SetValue(f"{position}")
        imp.entry_velocity.SetValue(f"{velocity}")
    
    lastUpdate = elapsed

    sendTelemetry("TargetPosition", position)
    sendTelemetry("TargetVelocity", velocity)

def toggle_sync():
    global play
    global node, network, start
    rate = 100 # Hz

    play = not play
    if play: 
        home()
        start = timer()
        node.rpdo[1]["SetModeOfOperation"].raw = 13  # Impedance Control Mode

        # Start sending RPDOs
        node.rpdo[1].start(1/rate)
        node.rpdo[2].start(1/rate)

        # Start SYNC thread
        network.sync.start(1/rate)
    else:  
        # Stop sending RPDOs
        node.rpdo[1].stop()
        node.rpdo[2].stop()

        # Stop SYNC thread
        network.sync.stop()
        node.sdo["SetModeOfOperation"].raw = 0  # Idle Control Mode

    # Print play/pause state
    print("Play" if play else "Pause")

class ImpedanceControlApp(wx.Frame):
    def __init__(self, parent, title):
        super().__init__(parent, title=title, size=(570, 500))

        panel = wx.Panel(self)

        # Create a menu bar
        menu_bar = wx.MenuBar()

        # Create a File menu
        file_menu = wx.Menu()
        exit_menu_item = file_menu.Append(wx.ID_EXIT, "Quit\tCtrl+Q", "Quit the application")
        menu_bar.Append(file_menu, "&File")

        # Bind the Exit menu item to the quit function
        self.Bind(wx.EVT_MENU, self.on_exit, exit_menu_item)

        # Set the menu bar
        self.SetMenuBar(menu_bar)

        row = 0
        rowheight = 40
        rowoffset = 10
        colwidth = 140
        coloffset = 10

        wx.StaticText(panel, label="CAN Port:", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.choice_port = wx.Choice(panel, choices=["can0", "can1", "can2", "can3"], pos=(1*colwidth+coloffset, row*rowheight+rowoffset))
        self.choice_port.SetSelection(0)
        wx.Button(panel, label="Scan Pucks", pos=(2*colwidth+coloffset, row*rowheight+rowoffset)).Bind(wx.EVT_BUTTON, self.scan_pucks)

        row = row + 1
        wx.StaticText(panel, label="Select ID:", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.choice_id = wx.Choice(panel, choices=["None"], pos=(1*colwidth+coloffset, row*rowheight+rowoffset))
        self.choice_id.SetSelection(0)
        self.choice_id.Bind(wx.EVT_CHOICE, self.select_id)

        # Create input fields
        row = row + 2
        wx.StaticText(panel, label="Target Position (cts):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_position = wx.TextCtrl(panel, value="0", pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        wx.StaticText(panel, label="Stiffness:", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_stiffness = wx.TextCtrl(panel, value=f"{stiffness:.4f}", pos=(3*colwidth+coloffset, row*rowheight+rowoffset))
        #self.entry_stiffness.Bind(wx.EVT_MOUSEWHEEL, self.stiffwheel)

        row = row + 1
        wx.StaticText(panel, label="Target Velocity (cts/s):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_velocity = wx.TextCtrl(panel, value="0", pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        wx.StaticText(panel, label="Damping:", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_damping = wx.TextCtrl(panel, value=f"{damping:.4f}", pos=(3*colwidth+coloffset, row*rowheight+rowoffset))

        row = row + 1
        wx.StaticText(panel, label="Target Torque (mNm):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_torque = wx.TextCtrl(panel, value="0", pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        # Buttons
        row = row + 1
        wx.Button(panel, label="Update Parameters", pos=(int(1.5*colwidth)+coloffset, row*rowheight+rowoffset)).Bind(wx.EVT_BUTTON, self.update_parameters)

        # Command type selection
        row = row + 2
        wx.StaticText(panel, label="Wave Type:", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.command_var = wx.RadioBox(panel, choices=["None", "Sinusoidal", "Square", "Triangular"], pos=(1*colwidth+coloffset, row*rowheight+rowoffset-20))
        self.command_var.SetSelection(0)
        self.command_var.Bind(wx.EVT_RADIOBOX, self.update_wave_type)

        row = row + 1
        wx.StaticText(panel, label="Amplitude (cts):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_amplitude = wx.TextCtrl(panel, value="4096", pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        wx.StaticText(panel, label="Period (s):", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_period = wx.TextCtrl(panel, value="5", pos=(3*colwidth+coloffset, row*rowheight+rowoffset))

        # Sync control
        row = row + 1
        wx.StaticText(panel, label="Control Off/On:", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.play_pause_button = OnOffButton(panel, -1, "", pos=(1*colwidth+coloffset, row*rowheight+rowoffset), size=(48, 30), initial=0, border=False)
        self.play_pause_button.Bind(EVT_ON_OFF, self.toggle_play_pause)

        # Add checkboxes for telemetry selection
        row = row + 1
        wx.StaticText(panel, label="Telemetry Selection:", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.checkbox_target_position = wx.CheckBox(panel, label="TargetPosition", pos=(1*colwidth+coloffset, row*rowheight+rowoffset))
        self.checkbox_target_velocity = wx.CheckBox(panel, label="TargetVelocity", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        row = row + 1
        self.checkbox_position_feedback = wx.CheckBox(panel, label="PositionFeedback", pos=(1*colwidth+coloffset, row*rowheight+rowoffset))
        self.checkbox_velocity_feedback = wx.CheckBox(panel, label="VelocityFeedback", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        self.checkbox_current_feedback = wx.CheckBox(panel, label="CurrentFeedback", pos=(3*colwidth+coloffset, row*rowheight+rowoffset))

        self.Show()

    def stiffwheel(self, event):
        global stiffness
        rotation = event.GetWheelRotation()  # Get the wheel rotation
        delta = 10  # Define the increment/decrement value for stiffness

        if rotation > 0:
            stiffness += delta  # Increase stiffness
        elif rotation < 0:
            stiffness -= delta  # Decrease stiffness

        # Update the text box with the new stiffness value
        self.entry_stiffness.SetValue(f"{stiffness:.4f}")
        print(f"Stiffness updated to: {stiffness}")
        
    def update_wave_type(self, event):
        global command_type
        command_type = self.command_var.GetStringSelection()
        print(f"Command type updated to: {command_type}")
        if command_type == "None":
            self.entry_position.Enable(True)
            self.entry_velocity.Enable(True)
        else:
            self.entry_position.Enable(False)
            self.entry_velocity.Enable(False)

        

    def update_parameters(self, event):
        global target_position, stiffness, target_velocity, damping, target_torque, amplitude, period, command_type
        try:
            target_position = int(self.entry_position.GetValue())
            stiffness = float(self.entry_stiffness.GetValue())
            target_velocity = int(self.entry_velocity.GetValue())
            damping = float(self.entry_damping.GetValue())
            target_torque = int(self.entry_torque.GetValue())
            
            configure_impedance_mode()
        except ValueError:
            wx.MessageBox("Invalid parameter values!", "Error", wx.OK | wx.ICON_ERROR)

    def toggle_play_pause(self, event):
        global amplitude, period
        amplitude = int(self.entry_amplitude.GetValue())
        period = float(self.entry_period.GetValue())
        toggle_sync()
    
    def on_exit(self, event):
        """Quit the application gracefully."""
        self.Close()

    def scan_pucks(self, event):  
        """Scan for Pucks on the CAN bus."""
        try:
            self.network.disconnect() # Close any open networks
        except:
          pass

        print("Establishing a new network...")
        self.network = canopen.Network()
        can_device = self.choice_port.GetStringSelection()

        try:
          if platform.system() == "Windows" or platform.system() == "Darwin":
            self.network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
          elif platform.system() == "Linux":
            self.network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)  

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

        try:
            # Think we need these  for scan to work...
            # This will attempt to read an SDO from nodes 1 - 127
            self.network.scanner.reset()
            self.network.scanner.search()
            time.sleep(0.5)

            for node_id in self.network.scanner.nodes:
                print("Found node %d!" % node_id) 

            # Populate the node choice list
            self.choice_id.SetItems([str(i) for i in self.network.scanner.nodes])

            # If we found at least one, select the first
            if len(self.network.scanner.nodes) > 0:
                self.choice_id.SetSelection(0)
                self.select_id(None)

        except:
            print('No CAN driver found!')
            msg = 'No CAN bus found! \nCheck connection and try again'
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return

    def select_id(self, event):  
        node_id = int(self.choice_id.GetString(self.choice_id.GetSelection()))
        print("Selected node = {0}".format(node_id))
        if node_id not in self.network:
          # Add our canopen node along with its object dictionary (for parsing)
          print("Adding new node: {0}".format(node_id))
          self.node = self.network.add_node(node_id, '../puck4.eds')
        else:  
          self.node = self.network[node_id]

        configure_impedance_mode()


def sendTelemetry(name, value):
    """Send telemetry data only if the corresponding checkbox is selected."""
    telemetry_map = {
        "TargetPosition": imp.checkbox_target_position.GetValue(),
        "TargetVelocity": imp.checkbox_target_velocity.GetValue(),
        "PositionFeedback": imp.checkbox_position_feedback.GetValue(),
        "VelocityFeedback": imp.checkbox_velocity_feedback.GetValue(),
        "CurrentFeedback": imp.checkbox_current_feedback.GetValue(),
    }

    if telemetry_map.get(name, False):
        now = time.time() * 1000
        msg = f"{name}:{now}:{value}|g"
        sock.sendto(msg.encode(), teleplotAddr)

if __name__ == "__main__":
    global node, imp
    # Initialize CANopen network
    network = canopen.Network()
    network.connect(bustype='socketcan', channel='can2', bitrate=1000000)
    node = network.add_node(5, '../puck4.eds')  # Replace with your node ID and EDS file

    stiffness = U32ToFloat(node.sdo["ImpCtrl"]["Stiffness"].raw)
    damping = U32ToFloat(node.sdo["ImpCtrl"]["Damping"].raw)

    configure_impedance_mode()

    app = wx.App(False)
    imp = ImpedanceControlApp(None, "Impedance Control GUI")
    app.MainLoop()

    # Stop SYNC and RPDOs if they were running
    if play:
        toggle_sync()

    # Idle
    node.sdo["SetModeOfOperation"].raw = 0

    network.disconnect()
