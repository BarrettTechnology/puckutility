#!/usr/bin/env python3
#
# Apply a step change in torque and record the motor's step response
#
# Usage:
#   step_response.py <can_device> <can_id>
#   Note: can0 is (usually) the first CAN device installed. Use this format for Windows, too.
#
# Dependencies:
#   python3 -m pip install canopen

import sys
import math
import platform
import time

import canopen
from timeit import default_timer as timer

period = 5 # seconds, for cyclic sync sinusoids

def configure_puck():
    global node

    # Read the existing PDOs from device (to allocate/populate local copy)
    print("Reading PDOs")
    node.tpdo.read()
    node.rpdo.read()

    # Each time we receive this PDO from the puck, execute a callback
    node.tpdo[1].add_callback(tpdo1_callback)
    node.tpdo[2].add_callback(tpdo2_callback)

    # Disable Heartbeats
    node.sdo["HeartbeatPeriod"].raw = 0

def tpdo1_callback(msg):
    global node
    
    # Store data
    status = node.tpdo[1]['StatusWord'].raw
    mode = node.tpdo[1]['ReadModeOfOperation'].raw
    pos = node.tpdo[1]['PositionFeedback'].raw

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

    sin = math.sin(2*math.pi/period * (timer() - start))

    # Toggle "new position setpoint" every other cycle.
    # This is cheating. We SHOULD be watching the StatusWord and waiting for 
    # the setpoint to be acknowledged before sending a new one.
    # This is ignored for trq/vel modes
    if node.rpdo[1]['ControlWord'].raw == 0x2F:
      node.rpdo[1]['ControlWord'].raw = 0x3F
    else:
      node.rpdo[1]['ControlWord'].raw = 0x2F

    node.rpdo[1]['TargetTorque'].raw = sin * maxtrq
    node.rpdo[2]['TargetVelocity'].raw = sin * maxvel
    node.rpdo[2]['TargetPosition'].raw = sin * maxpos


def OpEnable():
    # Clear faults, RTSO, OpEnabled
    print("Going OpEnabled")
    node.sdo["ControlWord"].raw = 0x80
    node.sdo["ControlWord"].raw = 0x06
    node.sdo["ControlWord"].raw = 0x0F

def end():
    global node
    print("Setting Mode = IDLE")
    node.sdo["SetModeOfOperation"].raw = 0

    network.disconnect()

    sys.exit()

def pt():
    print("1) Profile Torque")
    global node
    print("Setting Mode = Profile Torque")
    node.sdo["SetModeOfOperation"].raw = 4
    # Values in /1000 of rated torque
    for i in [100, 250, 500, 750, 1000, -1000, -750, -500, -250, -100, 0]:
        node.sdo["TargetTorque"].raw = i
        time.sleep(0.5)

def pv():
    print("2) Profile Velocity")
    global node
    print("Setting Mode = Profile Velocity")
    node.sdo["SetModeOfOperation"].raw = 3
    # Values in cts/s
    for i in [100, 1000, 10000, 20000, -20000, -10000, -1000, -100, 0]:
        node.sdo["TargetVelocity"].raw = i
        time.sleep(3)

def ppiad():
    print("3) Profile Position, Immediate, Absolute, Discrete")
    global node

    # Home the motor first
    home()

    print("Setting Mode = Profile Position")
    node.sdo["SetModeOfOperation"].raw = 1

    # Set ControlWord to 0x2F (Immediate positions)
    node.sdo["ControlWord"].raw = 0x2F
    
    waypoints = [ # [pos, v_cruise, v_final, acc, dec]
        [4096,10000,0, 20000,20000],
        [8192,10000,0, 20000,20000],
        [-8192,10000,0, 20000,20000],
        [-4096,10000,0, 20000,20000],
        [0,10000,0, 20000,20000],
    ]
    for i in waypoints:
        # Wait for StatusWord[12] == 0 (ready to receive new waypoint)
        while node.sdo["StatusWord"].raw & 0x1000:
            time.sleep(0.01)

        node.sdo["TargetPosition"].raw = i[0]
        node.sdo["ProfileVelocity"].raw = i[1]
        node.sdo["EndVelocity"].raw = i[2]
        node.sdo["Acceleration"].raw = i[3]
        node.sdo["Deceleration"].raw = i[4]

        # Set New Setpoint flag
        node.sdo["ControlWord"].raw = 0x3F

        # Wait for StatusWord[12] == 1 (setpoint acknowledged)
        while not (node.sdo["StatusWord"].raw & 0x1000):
            time.sleep(0.01)
        
        # Set ControlWord to 0x2F (clear new setpoint flag)
        node.sdo["ControlWord"].raw = 0x2F

        # Wait for StatusWord[10] == 1 (target reached)
        while not (node.sdo["StatusWord"].raw & 0x0400):
            time.sleep(0.01)

def ppias10():
    print("4) Profile Position, Immediate, Absolute, Streamed, 10 Hz")
    global node

    # Home the motor first
    home()

    # Set the profile parameters
    node.sdo["ProfileVelocity"].raw = 10000
    node.sdo["EndVelocity"].raw = 0
    node.sdo["Acceleration"].raw = 20000
    node.sdo["Deceleration"].raw = 20000

    print("Setting Mode = PP")
    node.rpdo[1]["SetModeOfOperation"].raw = 1

    # Set ControlWord to 0x2F (Immediate positions)
    node.rpdo[1]["ControlWord"].raw = 0x2F

    runpdo(10)

    # Idle the motor
    node.sdo["SetModeOfOperation"].raw = 0

def ppias100():
    print("5) Profile Position, Immediate, Absolute, Streamed, 100 Hz")
    global node

    # Home the motor first
    home()

    # Set the profile parameters
    node.sdo["ProfileVelocity"].raw = 10000
    node.sdo["EndVelocity"].raw = 0
    node.sdo["Acceleration"].raw = 20000
    node.sdo["Deceleration"].raw = 20000

    print("Setting Mode = PP")
    node.rpdo[1]["SetModeOfOperation"].raw = 1

    # Set ControlWord to 0x2F (Immediate positions)
    node.rpdo[1]["ControlWord"].raw = 0x2F

    runpdo(100)

    # Idle the motor
    node.sdo["SetModeOfOperation"].raw = 0   

def ppird():
    print("6) Profile Position, Immediate, Relative, Discrete")
    global node

    # Home the motor first
    home()

    print("Setting Mode = Profile Position")
    node.sdo["SetModeOfOperation"].raw = 1

    # Set ControlWord to 0x6F (Immediate Relative positions)
    node.sdo["ControlWord"].raw = 0x6F
    
    waypoints = [ # [relpos, v_cruise, v_final, acc, dec]
        [4096,10000,0, 20000,20000],
        [4096,10000,0, 20000,20000],
        [-4096,10000,0, 20000,20000],
        [-4096,10000,0, 20000,20000],
    ]
    for i in waypoints:
        # Wait for StatusWord[12] == 0 (ready to receive new waypoint)
        while node.sdo["StatusWord"].raw & 0x1000:
            time.sleep(0.01)
            
        node.sdo["TargetPosition"].raw = i[0]
        node.sdo["ProfileVelocity"].raw = i[1]
        node.sdo["EndVelocity"].raw = i[2]
        node.sdo["Acceleration"].raw = i[3]
        node.sdo["Deceleration"].raw = i[4]

        # Set New Setpoint flag
        node.sdo["ControlWord"].raw = 0x7F

        # Wait for StatusWord[12] == 1 (setpoint acknowledged)
        while not (node.sdo["StatusWord"].raw & 0x1000):
            time.sleep(0.01)
        
        # Set ControlWord to 0x6F (clear new setpoint flag)
        node.sdo["ControlWord"].raw = 0x6F

        # Wait for StatusWord[10] == 1 (target reached)
        while not (node.sdo["StatusWord"].raw & 0x0400):
            time.sleep(0.01)

def ppba():
    print("7) Profile Position, Buffered, Absolute")
    global node
    print("Setting Mode = Profile Position")
    node.sdo["SetModeOfOperation"].raw = 1

    # Set ControlWord to 0x0F (Buffered positions)
    node.sdo["ControlWord"].raw = 0x0F

    # Set Halt (do not execute the buffered setpoints yet)
    # Not yet implemented!
    
    waypoints = [ # [pos, v_cruise, v_final, acc, dec]
        [4096,10000,0, 20000,20000],
        [8192,10000,0, 20000,20000],
        [-8192,10000,0, 20000,20000],
        [-4096,10000,0, 20000,20000],
        [0,10000,0, 20000,20000],
    ]
    for i in waypoints:
        # Wait for StatusWord[12] == 0 (ready to receive new waypoint)
        while node.sdo["StatusWord"].raw & 0x1000:
            time.sleep(0.01)
        
        node.sdo["TargetPosition"].raw = i[0]
        node.sdo["ProfileVelocity"].raw = i[1]
        node.sdo["EndVelocity"].raw = i[2]
        node.sdo["Acceleration"].raw = i[3]
        node.sdo["Deceleration"].raw = i[4]

        # Set New Setpoint flag
        node.sdo["ControlWord"].raw = 0x3F

        # Wait for StatusWord[12] == 1 (setpoint acknowledged)
        while not (node.sdo["StatusWord"].raw & 0x1000):
            time.sleep(0.01)
        
        # Set ControlWord to 0x2F (clear new setpoint flag)
        node.sdo["ControlWord"].raw = 0x2F

    # Release Halt (execute the buffered setpoints)
    # Not yet implemented!

    # Wait for StatusWord[10] == 1 (target reached)
    while not (node.sdo["StatusWord"].raw & 0x0400):
        time.sleep(0.01)

def ppbr():
    print("8) Profile Position, Buffered, Relative")
    global node
    print("Setting Mode = Profile Position")
    node.sdo["SetModeOfOperation"].raw = 1

def runpdo(rate=100): # 100 Hz
    # Set up the Cyclic Sync timing (only used in Cyclic Sync modes)
    node.sdo["Cyclic"]["InterpolationPeriod"].raw = 1000 / rate # milliseconds
    node.sdo["Cyclic"]["InterpolationScale"].raw = -3 # milliseconds

    # Set the initial targets
    node.rpdo[1]['TargetTorque'].raw = 0
    node.rpdo[2]['TargetVelocity'].raw = 0
    node.rpdo[2]['TargetPosition'].raw = 0

    global start
    start = timer()

    # Start sending RPDOs
    node.rpdo[1].start(1/rate)
    node.rpdo[2].start(1/rate)

    # Start SYNC thread
    network.sync.start(1/rate)

    # Wait 10s (while the RPDOs are running)
    time.sleep(10)

    # Stop SYNC thread
    network.sync.stop()

    # Stop the RPDOs
    node.rpdo[1].stop()
    node.rpdo[2].stop()

def cst():
    print("9) Cyclic Synchronous Torque")
    global node

    # Set RPDO ControlWord to 0x0F (active)
    node.rpdo[1]["ControlWord"].raw = 0x0F

    print("Setting Mode = CST")
    node.rpdo[1]['SetModeOfOperation'].raw = 10

    runpdo()

    # Idle the motor
    node.sdo["SetModeOfOperation"].raw = 0


def csv():
    print("10) Cyclic Synchronous Velocity")
    global node

    # Set RPDO ControlWord to 0x0F (active)
    node.rpdo[1]["ControlWord"].raw = 0x0F

    print("Setting Mode = CSV")
    node.rpdo[1]["SetModeOfOperation"].raw = 9

    runpdo()

    # Idle the motor
    node.sdo["SetModeOfOperation"].raw = 0

def csp():
    print("11) Cyclic Synchronous Position")
    global node

    # Home the motor first
    home()

    # Set RPDO ControlWord to 0x0F (active)
    node.rpdo[1]["ControlWord"].raw = 0x0F
    
    print("Setting Mode = CSP")
    node.rpdo[1]["SetModeOfOperation"].raw = 8

    runpdo()

    # Idle the motor
    node.sdo["SetModeOfOperation"].raw = 0

def home():
    print("12) Homing")
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

if __name__ == "__main__":
    global node
    # Read the command arguments
    can_device = sys.argv[1]
    can_id = int(sys.argv[2])

    try:
      print("Establishing a new network...")
      network = canopen.Network()

      time.sleep(0.2) # Wait for any bus-off to clear

      if platform.system() == "Windows":
        network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
      elif platform.system() == "Linux":
        network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)

      print("Connection succeeded, adding CANopen node...")
      # Add our canopen node along with its object dictionary (for parsing)
      node = network.add_node(can_id, '../puck4.eds')

    except:
      print("Connection failed. Exiting.")
      pass  

    # Initialize
    configure_puck()

    OpEnable()

    # Map the inputs to the function blocks
    options = {
           0 : end,
           1 : pt,
           2 : pv,
           3 : ppiad,
           4 : ppias10,
           5 : ppias100,
           6 : ppird,
           7 : ppba,
           8 : ppbr,
           9 : cst,
           10 : csv,
           11 : csp,
           12 : home
    }

    while True:
        # Display menu
        print("\nOptions:\n")
        print("0) Exit")
        print("1) Profile Torque")
        print("2) Profile Velocity")
        print("3) Profile Position, Immediate, Absolute, Discrete")
        print("4) Profile Position, Immediate, Absolute, Streamed, 10 Hz")
        print("5) Profile Position, Immediate, Absolute, Streamed, 100 Hz")
        print("6) Profile Position, Immediate, Relative, Discrete")
        print("7) Profile Position, Buffered, Absolute")
        print("8) Profile Position, Buffered, Relative")
        print("9) Cyclic Synchronous Torque")
        print("10) Cyclic Synchronous Velocity")
        print("11) Cyclic Synchronous Position")
        print("12) Homing")
        
        num = int(input("\nYour choice: "))

        # Run the requested test
        options[num]()

    
