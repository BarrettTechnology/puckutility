#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import math
import canopen
import socket
from timeit import default_timer as timer
import wx
from onoffbutton import OnOffButton, EVT_ON_OFF  # Import the custom OnOffButton control

# Teleplot configuration
teleplotAddr = ("127.0.0.1", 47269)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Global variables for impedance parameters
target_position = 0
stiffness = 1000
target_velocity = 0
damping = 100
target_torque = 0
command_type = "sinusoidal"  # Default command type
play = False  # Play/Pause state
amplitude = 4096  # Default amplitude
period = 5  # Default period

def sendTelemetry(name, value):
    now = time.time() * 1000
    msg = f"{name}:{now}:{value}|g"
    sock.sendto(msg.encode(), teleplotAddr)

def OpEnable():
    # Clear faults, RTSO, OpEnabled
    print("Going OpEnabled")
    node.sdo["ControlWord"].raw = 0x80
    node.sdo["ControlWord"].raw = 0x06
    node.sdo["ControlWord"].raw = 0x0F

def configure_impedance_mode():
    global node

    # Read PDO configuration from the actuator
    node.tpdo.read()
    node.rpdo.read()

    # Initialize impedance parameters
    node.rpdo[2]["TargetPosition"].raw = target_position
    #node.rpdo[1]["Stiffness"].raw = stiffness
    node.rpdo[2]["TargetVelocity"].raw = target_velocity
    #node.rpdo[1]["Damping"].raw = damping
    node.rpdo[1]["TargetTorque"].raw = target_torque
    
    # Each time we receive this PDO from the puck, execute a callback
    node.tpdo[1].add_callback(tpdo1_callback)
    node.tpdo[2].add_callback(tpdo2_callback)

    # Disable Heartbeats
    node.sdo["HeartbeatPeriod"].raw = 0

    OpEnable()

    # Set RPDO ControlWord to 0x0F (active)
    node.rpdo[1]["ControlWord"].raw = 0x0F

    print("Setting Mode = Impedance Control")
    node.rpdo[1]["SetModeOfOperation"].raw = 13  # DS402 Impedance Control Mode

def generate_command(elapsed, period, amplitude):
    """
    Generate commanded position and velocity based on the selected command type.
    """
    if command_type == "sinusoidal":
        position = amplitude * math.sin(2 * math.pi * elapsed / period)
        velocity = (2 * math.pi * amplitude / period) * math.cos(2 * math.pi * elapsed / period)
    elif command_type == "square":
        position = amplitude if (elapsed % period) < (period / 2) else -amplitude
        velocity = 0  # Square wave has instantaneous velocity changes
    elif command_type == "triangular":
        phase = (elapsed % period) / period
        if phase < 0.5:
            position = amplitude * (4 * phase - 2)
            velocity = 4 * amplitude / period
        else:
            position = amplitude * (2 - 4 * phase)
            velocity = -4 * amplitude / period
    else:
        position = 0
        velocity = 0

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
    global period

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

    sendTelemetry("TargetPosition", position)
    sendTelemetry("TargetVelocity", velocity)
    sendTelemetry("TargetTorque", target_torque)

def toggle_sync():
    global play
    global node, network, start
    rate = 100 # Hz

    play = not play
    if play: 
        start = timer()

        # Start sending RPDOs
        node.rpdo[1].start(1/rate)
        node.rpdo[2].start(1/rate)

        # Start SYNC thread
        network.sync.start(1/rate)
    else:  # Stop sending RPDOs
        node.rpdo[1].stop()
        node.rpdo[2].stop()

        # Stop SYNC thread
        network.sync.stop()

    # Print play/pause state
    print("Play" if play else "Pause")

class ImpedanceControlApp(wx.Frame):
    def __init__(self, parent, title):
        super().__init__(parent, title=title, size=(400, 500))

        panel = wx.Panel(self)

        # Create a menu bar
        menu_bar = wx.MenuBar()

        # Create a File menu
        file_menu = wx.Menu()
        exit_menu_item = file_menu.Append(wx.ID_EXIT, "Exit\tCtrl+Q", "Quit the application")
        menu_bar.Append(file_menu, "&File")

        # Bind the Exit menu item to the quit function
        self.Bind(wx.EVT_MENU, self.on_exit, exit_menu_item)

        # Set the menu bar
        self.SetMenuBar(menu_bar)

        # Create input fields
        wx.StaticText(panel, label="Target Position (cts):", pos=(10, 10))
        self.entry_position = wx.TextCtrl(panel, value="0", pos=(150, 10))

        wx.StaticText(panel, label="Stiffness:", pos=(290, 10))
        self.entry_stiffness = wx.TextCtrl(panel, value="1000", pos=(400, 10))

        wx.StaticText(panel, label="Target Velocity (cts/s):", pos=(10, 50))
        self.entry_velocity = wx.TextCtrl(panel, value="0", pos=(150, 50))

        wx.StaticText(panel, label="Damping:", pos=(290, 50))
        self.entry_damping = wx.TextCtrl(panel, value="100", pos=(400, 50))

        wx.StaticText(panel, label="Target Torque (mNm):", pos=(10, 90))
        self.entry_torque = wx.TextCtrl(panel, value="0", pos=(150, 90))

        # Buttons
        wx.Button(panel, label="Update Parameters", pos=(220, 130)).Bind(wx.EVT_BUTTON, self.update_parameters)

        # Command type selection
        wx.StaticText(panel, label="Wave Type:", pos=(10, 220))
        self.command_var = wx.RadioBox(panel, choices=["None", "Sinusoidal", "Square", "Triangular"], pos=(150, 210))
        self.command_var.SetSelection(0)
        self.command_var.Bind(wx.EVT_RADIOBOX, self.update_wave_type)

        wx.StaticText(panel, label="Amplitude (cts):", pos=(10, 270))
        self.entry_amplitude = wx.TextCtrl(panel, value="4096", pos=(150, 270))

        wx.StaticText(panel, label="Period (s):", pos=(290, 270))
        self.entry_period = wx.TextCtrl(panel, value="5", pos=(400, 270))

        # Replace Play/Pause button with OnOffButton
        wx.StaticText(panel, label="SYNC Off/On:", pos=(10, 310))
        self.play_pause_button = OnOffButton(panel, -1, "", pos=(150, 310), size=(48, 30), initial=0, border=False)
        self.play_pause_button.Bind(EVT_ON_OFF, self.toggle_play_pause)

        self.Show()

    def update_wave_type(self, event):
        command_type = self.command_var.GetStringSelection().lower()
        print(f"Command type updated to: {command_type}")

    def update_parameters(self, event):
        global target_position, stiffness, target_velocity, damping, target_torque, amplitude, period, command_type
        try:
            target_position = int(self.entry_position.GetValue())
            stiffness = int(self.entry_stiffness.GetValue())
            target_velocity = int(self.entry_velocity.GetValue())
            damping = int(self.entry_damping.GetValue())
            target_torque = int(self.entry_torque.GetValue())
            amplitude = int(self.entry_amplitude.GetValue())
            period = float(self.entry_period.GetValue())
            #command_type = self.command_var.GetStringSelection().lower()
            configure_impedance_mode()
            #wx.MessageBox("Parameters updated successfully!", "Success", wx.OK | wx.ICON_INFORMATION)
        except ValueError:
            wx.MessageBox("Invalid parameter values!", "Error", wx.OK | wx.ICON_ERROR)

    def toggle_play_pause(self, event):
        #print("Toggling SYNC state")
        toggle_sync()
    
    def on_exit(self, event):
        """Quit the application gracefully."""
        self.Close()
    
if __name__ == "__main__":
    global node
    # Initialize CANopen network
    network = canopen.Network()
    network.connect(bustype='socketcan', channel='can2', bitrate=1000000)
    node = network.add_node(5, '../puck4.eds')  # Replace with your node ID and EDS file

    configure_impedance_mode()

    app = wx.App(False)
    ImpedanceControlApp(None, "Impedance Control GUI")
    app.MainLoop()

    network.disconnect()
