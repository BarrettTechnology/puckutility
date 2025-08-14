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
from pubsub import pub
from odometer import Odometer  # Import the Odometer control

from cffi import FFI

ffi = FFI()

# Declare the C types and functions you want to use
ffi.cdef("""
typedef struct {
	int type;
	float Q;
	float Fc;
	float peakGain;
	float a0, a1, a2, b1, b2;
	float z1, z2;
}biquadStruct;

typedef struct {
    int32_t cts[32];
    int32_t age[32];
    uint8_t head;
    uint8_t len;
    int32_t rate;
    int32_t vel;
    biquadStruct bq;
} velocity_ring_t;

void velocity_ring_init(velocity_ring_t *r, uint8_t len, int32_t rate);
void velocity_ring_add(velocity_ring_t *r, int32_t cts);
int32_t velocity_ring_eval(velocity_ring_t *r);
""")

# Load the shared library
lib = ffi.dlopen("./libvelocity_ring.so")

# Teleplot configuration
teleplotAddr = ("127.0.0.1", 47269)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

DEFAULT_AMPLITUDE = 4096 # cts
DEFAULT_PERIOD = 5.0  # seconds

command_type = "None"  # Default command type


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



class ImpedanceControlApp(wx.Frame):
    def __init__(self, parent, title):
        super().__init__(parent, title=title, size=(600, 600))

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

        # create a pubsub receiver
        pub.subscribe(self.updateDisplay, 'update')

        # Set up the velocity ring
        self.rate = 100  # Hz
        self.vring = ffi.new("velocity_ring_t *")
        lib.velocity_ring_init(self.vring,8, self.rate)
        self.velocity_estimate = 0

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
        self.target_position = 0
        wx.StaticText(panel, label="Target Position (cts):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_position = wx.TextCtrl(panel, value=str(self.target_position), pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        wx.StaticText(panel, label="Stiffness:", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        self.odometer_stiffness = Odometer(panel, pos=(3*colwidth+coloffset, row*rowheight+rowoffset), size=(120, 30), format="###.####", initial=0.0)

        row = row + 1
        self.target_velocity = 0
        wx.StaticText(panel, label="Target Velocity (cts/s):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_velocity = wx.TextCtrl(panel, value=str(self.target_velocity), pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        wx.StaticText(panel, label="Damping:", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        self.odometer_damping = Odometer(panel, pos=(3*colwidth+coloffset, row*rowheight+rowoffset), size=(120, 30), format="###.####", initial=0.0)

        row = row + 1
        self.target_torque = 0
        wx.StaticText(panel, label="Target Torque (mNm):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_torque = wx.TextCtrl(panel, value=str(self.target_torque), pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        # Buttons
        wx.Button(panel, label="Save As Defaults", pos=(3*colwidth+coloffset, row*rowheight+rowoffset)).Bind(wx.EVT_BUTTON, self.save_parameters)

        # Command type selection
        row = row + 2
        wx.StaticText(panel, label="Wave Type:", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.command_var = wx.RadioBox(panel, choices=["None", "Sinusoidal", "Triangular", "Square"], pos=(1*colwidth+coloffset, row*rowheight+rowoffset-20))
        self.command_var.SetSelection(0)
        self.command_var.Bind(wx.EVT_RADIOBOX, self.update_wave_type)

        row = row + 1
        self.amplitude = DEFAULT_AMPLITUDE
        wx.StaticText(panel, label="Amplitude (cts):", pos=(0*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_amplitude = wx.TextCtrl(panel, value=str(self.amplitude), style=wx.TE_PROCESS_ENTER, pos=(1*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_amplitude.Bind(wx.EVT_TEXT_ENTER, self.OnSetAmplitude)
        
        self.period = DEFAULT_PERIOD
        wx.StaticText(panel, label="Period (s):", pos=(2*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_period = wx.TextCtrl(panel, value=str(self.period), style=wx.TE_PROCESS_ENTER, pos=(3*colwidth+coloffset, row*rowheight+rowoffset))
        self.entry_period.Bind(wx.EVT_TEXT_ENTER, self.OnSetPeriod)
        

        # Sync control
        row = row + 1
        self.play = False
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
        row = row + 1
        self.checkbox_velocity_estimate = wx.CheckBox(panel, label="VelocityEstimate", pos=(1*colwidth+coloffset, row*rowheight+rowoffset))

        # Periodically update the value field
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnGetValue, self.timer)
        self.timer.Start(100)  # Update every 100ms

        self.Show()

    def OnGetValue(self, event):
        """
        Handles the event to display the current value of the odometer.
        """
        self.stiffness = self.odometer_stiffness.GetValue()
        self.damping = self.odometer_damping.GetValue()

    def OnSetAmplitude(self, event):
        """
        Handles the event to update the amplitude.
        """
        self.amplitude = int(self.entry_amplitude.GetValue())
        print(f"Amplitude set to: {self.amplitude}")

    def OnSetPeriod(self, event):
        """
        Handles the event to display the current value of the odometer.
        """
        self.period = float(self.entry_period.GetValue())
        print(f"Period set to: {self.period}")

    def generate_command(self, elapsed):
        """
        Generate commanded position and velocity based on the selected command type.
        """

        if command_type == "Sinusoidal":
            position = int(self.amplitude * math.sin(2 * math.pi * elapsed / self.period))
            velocity = int((2 * math.pi * self.amplitude / self.period) * math.cos(2 * math.pi * elapsed / self.period))
        elif command_type == "Square":
            position = int(self.amplitude if (elapsed % self.period) < (self.period / 2) else -self.amplitude)
            velocity = 0  # Square wave has instantaneous velocity changes
        elif command_type == "Triangular":
            phase = (elapsed % self.period) / self.period # Normalize phase to [0, 1]
            if phase < 0.5:
                position = int(self.amplitude * (4 * phase - 1))
                velocity = int(4 * self.amplitude / self.period)
            else:
                position = int(self.amplitude * (3 - 4 * phase))
                velocity = int(-4 * self.amplitude / self.period)
        else:
            position = self.target_position
            velocity = self.target_velocity

        return position, velocity

    def update_wave_type(self, event):
        global command_type

        if self.play is True:
            self.toggle_play_pause(None)
            self.home()
            self.toggle_play_pause(None)
        else:
            self.home()

        command_type = self.command_var.GetStringSelection()
        print(f"Command type updated to: {command_type}")
        if command_type == "None":
            self.entry_position.Enable(True)
            self.entry_velocity.Enable(True)
        else:
            self.entry_position.Enable(False)
            self.entry_velocity.Enable(False)

        

    def save_parameters(self, event):
        try:
            self.node.sdo['Save']['Single'].raw = 0x00238301  # Save the stiffness
            self.node.sdo['Save']['Single'].raw = 0x00238302  # Save the damping


        except ValueError:
            wx.MessageBox("Invalid parameter values!", "Error", wx.OK | wx.ICON_ERROR)

    def toggle_play_pause(self, event):
        
        self.play = not self.play
        if self.play: 
            self.home()
            self.start = timer()
            self.lastUpdate = 0
            self.node.rpdo[1]["SetModeOfOperation"].raw = 13  # Impedance Control Mode

            # Start sending RPDOs
            self.node.rpdo[1].start(1/self.rate)
            self.node.rpdo[2].start(1/self.rate)
            self.node.rpdo[3].start(1/self.rate)

            # Start SYNC thread
            self.network.sync.start(1/self.rate)
        else:  
            # Stop sending RPDOs
            self.node.rpdo[1].stop()
            self.node.rpdo[2].stop()
            self.node.rpdo[3].stop()

            # Stop SYNC thread
            self.network.sync.stop()
            self.node.sdo["SetModeOfOperation"].raw = 0  # Idle Control Mode

        # Print play/pause state
        print("Play" if self.play else "Pause")
    
    
    def on_exit(self, event):
        """Quit the application gracefully."""
        print("Exiting Impedance Control App...")

        # Stop SYNC and RPDOs if they were running
        if self.play:
            self.toggle_play_pause(None)

        # Idle
        try:
            self.node.sdo["SetModeOfOperation"].raw = 0
            self.network.disconnect()
        except:
            pass

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

        except:
            print('No CAN driver found!')
            msg = 'No CAN bus found! \nCheck connection and try again'
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return

        # If we found at least one, select the first
        if len(self.network.scanner.nodes) > 0:
            self.choice_id.SetSelection(0)
            self.select_id(None)

    def select_id(self, event): 

        node_id = int(self.choice_id.GetString(self.choice_id.GetSelection()))
        print("Selected node = {0}".format(node_id))
        if node_id not in self.network:
          # Add our canopen node along with its object dictionary (for parsing)
          print("Adding new node: {0}".format(node_id))
          self.node = self.network.add_node(node_id, '../puck4.eds')
        else:  
          self.node = self.network[node_id]

        stiffness = U32ToFloat(self.node.sdo["ImpCtrl"]["Stiffness"].raw)
        self.odometer_stiffness.SetValue(stiffness)  # Update odometer with stiffness
        damping = U32ToFloat(self.node.sdo["ImpCtrl"]["Damping"].raw)
        self.odometer_damping.SetValue(damping)  # Update odometer with damping

        self.configure_impedance_mode()

    def configure_impedance_mode(self):
        # Read PDO configuration from the actuator
        self.node.tpdo.read()
        self.node.rpdo.read()

        # Each time we receive this PDO from the puck, execute a callback
        self.node.tpdo[1].add_callback(self.tpdo1_callback)
        self.node.tpdo[2].add_callback(self.tpdo2_callback)

        # Disable Heartbeats
        self.node.sdo["HeartbeatPeriod"].raw = 0

        # Set up the Cyclic Sync timing (only used in Cyclic Sync modes)
        self.node.sdo["Cyclic"]["InterpolationPeriod"].raw = 1000 / self.rate # milliseconds
        self.node.sdo["Cyclic"]["InterpolationScale"].raw = -3 # milliseconds

        self.OpEnable()

        # Set RPDO ControlWord to 0x0F (active)
        self.node.rpdo[1]["ControlWord"].raw = 0x0F
    
    def tpdo1_callback(self, msg):
        # Store data
        status = self.node.tpdo[1]['StatusWord'].raw
        mode = self.node.tpdo[1]['ReadModeOfOperation'].raw
        self.position_feedback = self.node.tpdo[1]['PositionFeedback'].raw

        # Add new encoder position to the ring buffer
        lib.velocity_ring_add(self.vring, self.position_feedback)

        # Evaluate velocity using the ring buffer
        self.velocity_estimate = lib.velocity_ring_eval(self.vring)
        

    def tpdo2_callback(self, msg):

        maxtrq = 1000     # /1000 of rated torque
        maxvel = 20000    # cts/sec
        maxpos = 5 * 4096 # 5 revolutions
        
        # Store data
        self.velocity_feedback = self.node.tpdo[2]['VelocityFeedback'].raw
        self.current_feedback = self.node.tpdo[2]['CurrentFeedback'].raw

        # Calculate new pos/vel commands based on wave type
        elapsed = timer() - self.start
        self.target_position, self.target_velocity = self.generate_command(elapsed)
        self.node.rpdo[2]["TargetPosition"].raw = self.target_position
        self.node.rpdo[2]["TargetVelocity"].raw = self.target_velocity
        self.node.rpdo[1]["TargetTorque"].raw = self.target_torque

        self.node.rpdo[3]["ImpCtrl.Stiffness"].raw = FloatToU32(self.stiffness)
        self.node.rpdo[3]["ImpCtrl.Damping"].raw = FloatToU32(self.damping)

        # Limit the wx textctrl update to 10Hz
        if elapsed > self.lastUpdate + 0.1 and command_type != "None":
            self.lastUpdate = elapsed
            # We can't update the GUI in this thread safely, so use CallAfter and pubsub
            wx.CallAfter(self.publishData, self.target_position, self.target_velocity)
        
        self.sendTelemetry()

    def publishData(self, pos, vel):
        pub.sendMessage('update', arg={'pos': pos, 'vel': vel})
    
    def updateDisplay(self, arg):
        self.entry_position.SetValue(f"{arg['pos']}")
        self.entry_velocity.SetValue(f"{arg['vel']}")

    def sendTelemetry(self):
        """Send telemetry data for all checked checkboxes in a single packet."""
        telemetry_map = {
            "TargetPosition": (self.checkbox_target_position.GetValue(), self.target_position),
            "TargetVelocity": (self.checkbox_target_velocity.GetValue(), self.target_velocity),
            "PositionFeedback": (self.checkbox_position_feedback.GetValue(), self.position_feedback),
            "VelocityFeedback": (self.checkbox_velocity_feedback.GetValue(), self.velocity_feedback),
            "CurrentFeedback": (self.checkbox_current_feedback.GetValue(), self.current_feedback),
            "VelocityEstimate": (self.checkbox_velocity_estimate.GetValue(), self.velocity_estimate)
        }

        # Collect telemetry data for checked checkboxes
        telemetry_data = []
        now = time.time() * 1000  # Current timestamp in milliseconds
        for name, (is_checked, value) in telemetry_map.items():
            if is_checked:
                telemetry_data.append(f"{name}:{now}:{value}")

        # Send all telemetry data in a single packet
        if telemetry_data:
            packet = "\n".join(telemetry_data)  # Combine all telemetry data into a single string
            # print(f"Sending telemetry: {packet}")
            sock.sendto(packet.encode(), teleplotAddr)

    def OpEnable(self):
        # Clear faults, RTSO, OpEnabled
        print("Going OpEnabled")
        self.node.sdo["ControlWord"].raw = 0x80
        self.node.sdo["ControlWord"].raw = 0x06
        self.node.sdo["ControlWord"].raw = 0x0F

    def home(self):
        print("Setting Mode = Homing")
        self.node.sdo["ControlWord"].raw = 0x0F # Clear any mode-specific bits
        self.node.sdo["SetModeOfOperation"].raw = 6
        self.node.sdo["HomingOffset"].raw = 0 # Initialize position to zero
        self.node.sdo["HomingMethod"].raw = 37 # Home immediate, no limit switch
        self.node.sdo["ControlWord"].raw = 0x1F # Start homing
        while not (self.node.sdo["StatusWord"].raw & 0x1000): # Wait for homing complete
            time.sleep(0.1)
        self.node.sdo["ControlWord"].raw = 0x0F # Clear homing flag

if __name__ == "__main__":
    app = wx.App(False)
    imp = ImpedanceControlApp(None, "Impedance Control GUI")
    app.MainLoop()

