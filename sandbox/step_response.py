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
#   python3 -m pip install numpy

import sys
import math
import platform
import time

import canopen
import numpy


def _tolerate_tx_backpressure(network, timeout=0.5):
    """Give outgoing frames a real send timeout.

    canopen's Network.send_message calls bus.send(msg) with no timeout, so
    python-can's socketcan backend falls back to timeout=0 and polls the socket
    with select(..., 0). The first moment of TX backpressure then raises
    "Transmit buffer full" -- and the gs_usb TX URB pool is shallow enough that
    a burst of frames (e.g. scanner.search()'s 127) overruns it. Waiting briefly
    for queue space costs nothing on a healthy bus.
    """
    bus = getattr(network, "bus", None)
    if bus is None:
        return
    _orig_send = bus.send

    def _send_waiting(msg, *args, **kwargs):
        # Only fill in a timeout the caller didn't supply -- a positional
        # timeout would collide with the keyword.
        if not args and kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return _orig_send(msg, *args, **kwargs)

    bus.send = _send_waiting

# Number of records logged during step response
RECORDS = 5000 

def configure_puck(node):

    # Read the existing PDOs from device (to allocate/populate local copy)
    print("Reading PDOs")
    node.tpdo.read()
    node.rpdo.read()

    # PDOs are numbered 1-4
    # Clear the local copy of the PDO configs
    print("Clearing PDOs")
    for i in (1,2,3,4):
        node.tpdo[i].clear()
        node.rpdo[i].clear()

    print("Configuring TPDO4 for Step Response")
    node.tpdo[4].add_variable('Alpha','Raw')
    node.tpdo[4].add_variable('Beta','Raw')
    node.tpdo[4].add_variable('Theta_e')
    node.tpdo[4].add_variable('TargetTorque')
    node.tpdo[4].trans_type = 0 # Tx on every SYNC
    node.tpdo[4].enabled = True

    # Write the new TPDO config to the device
    print("Writing TPDOs")
    node.tpdo.save()

    # Write the new (empty) RPDO config to the device
    print("Writing RPDOs")
    node.rpdo.save()

    # Each time we receive this PDO from the puck, execute a callback
    node.tpdo[4].add_callback(tpdo_callback)

    # Disable Heartbeats
    node.sdo["HeartbeatPeriod"].raw = 0

def tpdo_callback(msg):
    global node
    global record_count
    global data

    # Store data into 2D numpy array
    data[record_count,2] = node.tpdo[4]['Alpha.Raw'].raw
    data[record_count,3] = node.tpdo[4]['Beta.Raw'].raw
    data[record_count,4] = node.tpdo[4]['Theta_e'].raw
    data[record_count,5] = node.tpdo[4]['TargetTorque'].raw

    record_count += 1
    if record_count != RECORDS:
        network.sync.transmit()


if __name__ == "__main__":
    global node
    global record_count
    global data

    # timestamp, theta_e, alpha.raw, beta.raw, theta_e.raw, torque.raw, alpha.cal, beta.cal, q_fbk, d_fbk, q_ref
    data = numpy.empty([RECORDS,11])

    # Keep track of the number of records received
    record_count = 0

    # Read the command arguments
    can_device = sys.argv[1]
    can_id = int(sys.argv[2])

    try:
      print("Establishing a new network...")
      network = canopen.Network()

      time.sleep(0.2) # Wait for any bus-off to clear

      if platform.system() == "Windows":
        network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
        _tolerate_tx_backpressure(network)
      elif platform.system() == "Linux":
        network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)
        _tolerate_tx_backpressure(network)

      print("Connection succeeded, adding CANopen node...")
      # Add our canopen node along with its object dictionary (for parsing)
      node = network.add_node(can_id, 'puck3.eds')

    except:
      pass  

    # Initialize
    configure_puck(node)

    # Read actuator parameters required for future calcs
    alpha_bias = node.sdo['Alpha']['Bias'].raw
    alpha_gainfactor = node.sdo['Alpha']['Gainfactor'].raw
    alpha_shunt = node.sdo['Alpha']['Shunt'].raw # mOhms
    alpha_gain = node.sdo['Alpha']['Gain'].raw
    beta_bias = node.sdo['Beta']['Bias'].raw
    beta_gainfactor = node.sdo['Beta']['Gainfactor'].raw # 
    pwm_freq = node.sdo['Amp']['Frequency'].raw # Hz
    motor_kt = node.sdo['Calibration']['kt'].raw # Motor kt (mNm/A)
    motor_rated_torque = node.sdo['RatedTorque'].raw # Motor peak/rated torque (mNm)
    motor_ical = node.sdo['Calibration']['i_cal'].raw # Calibration current (mA)
    motor_ipeak = node.sdo['Calibration']['i_peak'].raw # Motor peak current (mA)

    # Run
    
    # Clear faults, RTSO, OpEnabled
    print("Going OpEnabled")
    node.sdo["ControlWord"].raw = 0x80
    node.sdo["ControlWord"].raw = 0x06
    node.sdo["ControlWord"].raw = 0x0F

    print("Setting Mode = Torque")
    node.sdo["SetModeOfOperation"].raw = 4
    trq_cmd = motor_ical * motor_kt / motor_rated_torque # [-1000..+1000]
    #trq_cmd = motor_ical * 1000.0 / motor_ipeak # [-1000..+1000]
    print("Collecting step-response data with q-axis command = {0}".format(trq_cmd))
    #node.sdo["TargetTorque"].raw = trq_cmd

    node.sdo['Execute']['Value'].raw = trq_cmd
    node.sdo['Execute']['Function'].raw = 1 # Initiate Step Response

    # Wait for mode to return to IDLE (0)
    while node.sdo["ReadModeOfOperation"].raw != 0:
        time.sleep(0.1)
        
    # Start the SYNC avalanche
    network.sync.transmit()

    # Wait until all records are received
    while record_count != RECORDS:
        time.sleep(0.1)

    network.disconnect()

    # Perform calculations
    ma_per_ct = 3.3 / 4096 * 1000 / alpha_shunt * 1000 / alpha_gain * 1000

    print("ma_per_ct={0:0.2f}, motor_ical={1}".format(ma_per_ct, motor_ical))
    
    print("Calculating...")
    for i in range(RECORDS):
        # time_stamp (s)
        data[i,0] = i * 5.0 / pwm_freq
        # theta_e (rad)
        data[i,1] = data[i,4] * 3.14159 / 32768.0 
        # alpha_fbk_ma
        data[i,6] = (alpha_bias - data[i,2]) * alpha_gainfactor / 4096.0 * ma_per_ct
        # beta_fbk_ma
        data[i,7] = (beta_bias - data[i,3]) * beta_gainfactor / 4096.0 * ma_per_ct
        # q_fbk_ma = -a sin + b cos
        data[i,8] = -data[i,6] * math.sin(data[i,1]) + data[i,7] * math.cos(data[i,1])
        # d_fbk_ma = a cos + b sin
        data[i,9] = data[i,6] * math.cos(data[i,1]) + data[i,7] * math.sin(data[i,1])
        # q_ref_ma
        #data[i,10] = data[i,5] / 1000.0 * motor_ipeak
        data[i,10] = data[i,5] * motor_rated_torque / motor_kt

    print("Finished calculations!")
    numpy.savetxt("foo.csv", data, fmt="%f", delimiter=",")

    # Generate graph(s)
