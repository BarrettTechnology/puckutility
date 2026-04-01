#!/bin/env python3
"""
Barrett — Multi-motor JEMPC trajectory playback via CAN FD.

Plays back multiple joints from a JSONL trajectory file on multiple CAN motors
using cyclic-sync position mode over CAN FD. Edit the CONFIGURATION section below to
map CAN node IDs to joint names, motor parameters, and run options.

Usage:
    python main.py <can_channel> <rate_hz> <num_cycles> <jsonl_file>
    Example: python main.py can0 50 5 spot_trot_slow.jsonl
"""

import can
import canopen
import struct
import time
import queue
import json
import os
import argparse
import threading
from threading import Event
import logging

# Suppress python-canopen warnings about PDOs exceeding standard 64-bit limits (since we use CAN FD 64-byte limits)
logging.getLogger('canopen.pdo.base').setLevel(logging.ERROR)

# Fix python-canopen bug: PdoMap.save() uses SdoArray.values() to iterate mapping entries, but
# SdoArray.__iter__ reads the count from the DEVICE dynamically. After save() writes count=0
# to unlock the mapping, iterating values() reads back count=0 and yields nothing — so no
# mapping entries are ever written to the device. Fix: use an explicit enumerate loop instead.
def _pdo_save_fixed(self):
    from canopen.pdo.base import PDO_NOT_VALID, RTR_NOT_ALLOWED
    from canopen.sdo import SdoAbortedError
    if self.cob_id is None:
        return
    self.com_record[1].raw = (
        self.cob_id | PDO_NOT_VALID | (RTR_NOT_ALLOWED if not self.rtr_allowed else 0)
    )
    if self.trans_type is not None and self.com_record[2].writable:
        self.com_record[2].raw = self.trans_type
    if self.inhibit_time is not None and self.com_record[3].writable:
        self.com_record[3].raw = self.inhibit_time
    if self.event_timer is not None and self.com_record[5].writable:
        self.com_record[5].raw = self.event_timer
    if self.sync_start_value is not None and self.com_record[6].writable:
        self.com_record[6].raw = self.sync_start_value
    try:
        self.map_array[0].raw = 0
    except SdoAbortedError:
        self._fill_map(self.map_array[0].raw)
    for i, var in enumerate(self.map, start=1):
        entry = self.map_array[i]
        if not entry.od.writable:
            continue
        if getattr(self.pdo_node.node, "curtis_hack", False):
            entry.raw = var.index | var.subindex << 16 | var.length << 24
        else:
            entry.raw = var.index << 16 | var.subindex << 8 | var.length
    try:
        self.map_array[0].raw = len(self.map)
    except SdoAbortedError as e:
        if e.code != 0x06010002:
            raise
    self._update_data_size()
    if self.enabled:
        self.com_record[1].raw = self.cob_id | (RTR_NOT_ALLOWED if not self.rtr_allowed else 0)
        self.subscribe()

import canopen.pdo.base
canopen.pdo.base.PdoMap.save = _pdo_save_fixed

from RealNetwork import RealNetwork
from HelperFunctions import rad2counts, counts2rad, counts2deg, float_to_ieee_decimal

# ============ CONFIGURATION ============ 
from node_config import NODE_CONFIG
# ===========================================

# Shared positions list for the main loop to update and RPDO callback to send
shared_pos_counts = []


def load_trajectory_multi(filename, node_config):
    """Load trajectory data from a JSONL file for multiple joints.
    Returns times array and a dictionary of positions per node_id.
    """
    times = []
    positions = {node_id: [] for node_id in node_config}
    available_joints = set()
    
    joint_map = {node_id: cfg["joint_name"] for node_id, cfg in node_config.items()}

    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            record = json.loads(line)
            jp = record["joint_positions"]
            available_joints.update(jp.keys())
            
            times.append(record["time"])
            for node_id, joint_name in joint_map.items():
                if joint_name in jp:
                    positions[node_id].append(jp[joint_name])
                else:
                    # Fallback to 0.0 if missing at a specific timestamp
                    positions[node_id].append(0.0)

    if not times:
        raise ValueError(f"No data found in {filename}")
        
    for node_id, joint_name in joint_map.items():
        if joint_name not in available_joints:
            print(f"Warning: Joint '{joint_name}' (Node {node_id}) not found in trajectory.")
            print(f"Available joints: {', '.join(sorted(available_joints))}")
            
    print(f"Loaded {len(times)} samples for {len(joint_map)} mapped joints "
          f"(t=[{times[0]:.3f} .. {times[-1]:.3f}] s)")
          
    return times, positions


def configure_motor(node, node_id, config, homing_offset_rad, period, rpdo_slot_index, total_rpdo_slots):
    """Configure motor via SDO: home, PD params, TPDO1, RPDO1, and CSP mode."""
    tk_per_rev = config["tk_per_rev"]
    invert = -1 if config.get("invert_position", False) else 1
    
    node.sdo.RESPONSE_TIMEOUT = 2.0

    # Pre-operational for configuration
    node.nmt.state = "PRE-OPERATIONAL"
    time.sleep(0.1)

    # Disable heartbeat
    try:
        node.sdo["HeartbeatPeriod"].raw = 0
    except Exception as e:
        print(f"  Warning: could not disable heartbeat on node {node_id}: {e}")

    # Enable drive
    node.sdo["ControlWord"].raw = 0x80  # clear faults
    node.sdo["ControlWord"].raw = 0x06  # ready to switch on
    node.sdo["ControlWord"].raw = 0x0F  # operation enabled

    # Home
    print(f"Node {node_id}: Homing motor...")
    node.sdo["SetModeOfOperation"].raw = 6  # HOMING
    if homing_offset_rad != 0.0:
        node.sdo["HomingOffset"].raw = rad2counts(homing_offset_rad * invert, tk_per_rev)
    node.sdo["ControlWord"].raw = 0x1F  # start homing

    timeout = 2.0
    start = time.time()
    while time.time() - start < timeout:
        sw = node.sdo["StatusWord"].raw
        if sw & (1 << 12):
            print(f"Node {node_id}: Homing complete.")
            break
        time.sleep(0.01)
    else:
        print(f"Node {node_id}: Warning: homing timed out (StatusWord={hex(sw)})")

    # Configure PD parameters
    print(f"Node {node_id}: Configuring PD parameters...")
    if "Position_Kp" in config:
        node.sdo['Poscontrol'][1].raw = config["Position_Kp"]
    if "Velocity_Kp" in config:
        v_kp = float_to_ieee_decimal(config["Velocity_Kp"])
        node.sdo['Velcontrol'][1].raw = v_kp
    if "Velocity_Ki" in config:
        v_ki = float_to_ieee_decimal(config["Velocity_Ki"])
        node.sdo['Velcontrol'][2].raw = v_ki
    if "Velocity_Filter" in config:
        node.sdo['Filter'][1].raw = config["Velocity_Filter"]

    # Configure TPDO1 using python-canopen structure
    tpdo_cob_id = 0x180 | node_id
    print(f"Node {node_id}: Configuring TPDO1 (COB-ID {hex(tpdo_cob_id)})...")
    
    node.tpdo.read()
    
    # Stop and clear all existing TPDOs
    for i in range(1, 5):
        node.tpdo[i].stop()
        node.tpdo[i].clear()
        
    # Map desired variables to TPDO1
    node.tpdo[1].add_variable('StatusWord')             # 0x6041
    node.tpdo[1].add_variable('ReadModeOfOperation')    # 0x6061
    node.tpdo[1].add_variable('PositionFeedback')       # 0x6064
    node.tpdo[1].add_variable('VelocityFeedback')       # 0x606C
    node.tpdo[1].add_variable('CurrentFeedback')        # 0x6078
    node.tpdo[1].add_variable('Amplifier', 'Temperature') # 0x3000, 2
    
    node.tpdo[1].trans_type = 1                         # Sync
    node.tpdo[1].cob_id = tpdo_cob_id
    node.tpdo[1].enabled = True
    
    # Save the configuration to the node
    node.tpdo.save()

    # Configure RPDO1 using python-canopen structure (COB-ID 0x200 for all)
    rpdo_cob_id = 0x200
    print(f"Node {node_id}: Configuring RPDO1 (COB-ID {hex(rpdo_cob_id)})...")
    
    node.rpdo.read()
    
    # Stop and clear all existing RPDOs
    for i in range(1, 5):
        node.rpdo[i].stop()
        node.rpdo[i].clear()
        
    # Map one slot per active node; this node's TargetPosition is at rpdo_slot_index
    for i in range(total_rpdo_slots):
        if i == rpdo_slot_index:
            node.rpdo[1].add_variable('TargetPosition')  # 0x607A
        else:
            # Map a 32-bit dummy entry (Object 0x0007, sub-index 0)
            # 0x0007 is the CiA 301 standard definition for a 32-bit dummy padding object
            node.rpdo[1].add_variable(0x0007, 0, 32)
            
    node.rpdo[1].trans_type = 1                          # Sync
    node.rpdo[1].cob_id = rpdo_cob_id
    node.rpdo[1].enabled = True
    
    # Save configuration to the node
    node.rpdo.save()

    # Set Cyclic Sync Position mode
    print(f"Node {node_id}: Setting CSP mode...")
    #node.sdo["ControlWord"].raw = 0x80
    #node.sdo["ControlWord"].raw = 0x06
    #node.sdo["ControlWord"].raw = 0x0F
    node.sdo['Cyclic'][1].raw = int(period * 1000)   # interpolation period (ms)
    node.sdo['Cyclic'][2].raw = -3   # scale: 10^-3 -> ms
    node.sdo["SetModeOfOperation"].raw = 8  # CYCLIC_SYNC_POSITION

    mode = node.sdo["ReadModeOfOperation"].raw
    if mode != 8:
        raise RuntimeError(f"Node {node_id}: Failed to set CSP mode (got mode {mode})")
    print(f"Node {node_id}: CSP mode active.")

    # Go operational
    node.nmt.state = "OPERATIONAL"
    time.sleep(0.1)


def return_to_start_position(nodes, first_node, positions):
    """Gracefully return all nodes to their initial trajectory positions using Profile Position mode."""
    # Allow the final CSP position to be transmitted repeatedly for a few cycles.
    # This drops the interpolator's calculated velocity to 0, preventing the
    # motor from extrapolating and "drifting" when we stop the RPDO.
    return
    import time
    time.sleep(0.2)

    try:
        first_node.rpdo[1].stop()
    except Exception:
        pass
    
    print("Returning all nodes to initial positions...")
    
    # Prepare the initial positions
    initial_pos_counts = [0] * 16
    for i in range(1, 17):
        if i in NODE_CONFIG:
            cfg = NODE_CONFIG[i]
            tk = cfg["tk_per_rev"]
            invert = -1 if cfg.get("invert_position", False) else 1
            pos_rad = float(positions[i][0]) if len(positions[i]) > 0 else 0.0
            initial_pos_counts[i - 1] = int(rad2counts(pos_rad, tk) * invert)
    
    # Configure Profile Position mode and parameters for all nodes
    for node_id, node in nodes.items():
        node.sdo["SetModeOfOperation"].raw = 1  # PROFILE_POSITION
        
        # Set ControlWord to 0x2F (Immediate positions, enable operation)
        node.sdo["ControlWord"].raw = 0x2F
        
    for node_id, node in nodes.items():
        # Wait for a fresh TPDO showing SW[12] == 0 (ready for new setpoint)
        while True:
            node.tpdo[1].wait_for_reception(timeout=0.5)
            if not (node.tpdo[1]['StatusWord'].raw & 0x1000):
                break

        # Set trajectory parameters (safe defaults)
        node.sdo["TargetPosition"].raw = initial_pos_counts[node_id - 1]
        node.sdo["ProfileVelocity"].raw = 10000
        node.sdo["EndVelocity"].raw = 0
        node.sdo["Acceleration"].raw = 20000
        node.sdo["Deceleration"].raw = 20000

        # Set New Setpoint flag (bit 4 = New Setpoint, bit 5 = Change Immediately, absolute)
        node.sdo["ControlWord"].raw = 0x3F

    # Wait for all nodes to acknowledge the setpoint
    for node_id, node in nodes.items():
        # Wait for a fresh TPDO showing SW[12] == 1 (setpoint acknowledged)
        while True:
            node.tpdo[1].wait_for_reception(timeout=0.5)
            if node.tpdo[1]['StatusWord'].raw & 0x1000:
                break

        # Clear New Setpoint flag
        node.sdo["ControlWord"].raw = 0x2F

    # Wait for all nodes to reach their targets
    print("Waiting for motors to reach targets...")
    start_wait = time.time()
    timeout = 10.0

    while time.time() - start_wait < timeout:
        all_reached = True
        for node_id, node in nodes.items():
            node.tpdo[1].wait_for_reception(timeout=0.5)
            # In Profile Position mode, Target Reached is bit 10 (0x0400)
            if not (node.tpdo[1]['StatusWord'].raw & 0x0400):
                all_reached = False
        if all_reached:
            print("All motors successfully reached initial positions.")
            break
    else:
        print("Warning: Timed out waiting for motors to reach initial positions.")


def main():
    global shared_pos_counts
    
    parser = argparse.ArgumentParser(
        description="Multi-motor JEMPC trajectory playback via CAN FD.",
        usage="python3 main.py <can_channel> <num_cycles> <jsonl_file>"
    )
    parser.add_argument("can_channel", type=str, help="The CAN channel to use (e.g., can0)")
    parser.add_argument("num_cycles", type=int, help="Number of cycles to run (0 for infinite)")
    parser.add_argument("jsonl_file", type=str, help="Trajectory JSONL file to use")
    
    args = parser.parse_args()
    
    can_channel = args.can_channel
    num_loops = args.num_cycles
    trajectory_file = args.jsonl_file

    if not NODE_CONFIG:
        print("Error: NODE_CONFIG is empty. Please configure at least one node in node_config.py.")
        return

    # --- Load trajectory ---
    times, positions = load_trajectory_multi(trajectory_file, NODE_CONFIG)

    # Determine period from times array
    if len(times) > 1:
        period = float(times[1] - times[0])
    else:
        period = 0.02  # Default to 50Hz if only one point

    rate_hz = 1.0 / period if period > 0 else 0

    # --- Create queues ---
    raw_data_q = queue.Queue()

    # --- Create network ---
    network = RealNetwork(raw_data_q, fd=True, can_channel=can_channel)
    print(f"Using REAL CAN FD network on channel {can_channel}")

    # Determine active nodes: in NODE_CONFIG AND detected on bus, sorted by ID
    active_node_ids = sorted(nid for nid in NODE_CONFIG if nid in network.scanner.nodes)
    rpdo_slots = {nid: i for i, nid in enumerate(active_node_ids)}
    num_rpdo_slots = len(active_node_ids)
    global shared_pos_counts
    shared_pos_counts = [0] * num_rpdo_slots

    for nid in NODE_CONFIG:
        if nid not in network.scanner.nodes:
            print(f"Warning: Configured Node {nid} not detected on bus, skipping.")

    if not active_node_ids:
        print("Error: No configured nodes found on bus.")
        return

    print(f"Active nodes (RPDO1 slot order): {active_node_ids}")

    nodes = {}
    for node_id in active_node_ids:
        config = NODE_CONFIG[node_id]
        node = network.add_node(node_id, "puck4.eds")

        # Use the first position in the trajectory as the homing offset
        homing_offset = float(positions[node_id][0]) if len(positions[node_id]) > 0 else 0.0
        configure_motor(node, node_id, config, homing_offset, period, rpdo_slots[node_id], num_rpdo_slots)
        nodes[node_id] = node

    # Start SYNC
    print(f"Starting SYNC with period {period:.4f} s ({rate_hz:.2f} Hz)")
    network.sync.start(period)

    # --- Set up RPDO transmission via TPDO callback ---
    rpdo_cob_id = 0x200
    first_node_id = active_node_ids[0]
    first_node = nodes[first_node_id]

    cycle_complete_event = Event()
    trajectory_state = {"index": 0, "start_time": time.time()}
    num_points = len(times)

    def populate_rpdo_data(msg):
        idx = trajectory_state["index"]
        if idx >= num_points:
            return  # Cycle complete, stop updating RPDO from trajectory

        # Prepare one target position per active node, in slot order
        new_pos_counts = [0] * num_rpdo_slots
        for nid in active_node_ids:
            cfg = NODE_CONFIG[nid]
            tk = cfg["tk_per_rev"]
            invert = -1 if cfg.get("invert_position", False) else 1
            pos_rad = float(positions[nid][idx])
            pos_counts = int(rad2counts(pos_rad, tk) * invert)
            new_pos_counts[rpdo_slots[nid]] = pos_counts

        # Pack into the PdoMap data buffer, then call update() to propagate to
        # the running CAN task. Assigning .data alone does not call _task.update().
        first_node.rpdo[1].data = bytearray(struct.pack(f'<{num_rpdo_slots}i', *new_pos_counts))
        first_node.rpdo[1].update()

        global shared_pos_counts
        shared_pos_counts = new_pos_counts

        trajectory_state["index"] += 1
        if trajectory_state["index"] >= num_points:
            cycle_complete_event.set()

    # Pre-populate RPDO with the idx=0 positions so the periodic task's very
    # first frame commands the already-homed positions rather than zeros.
    # Without this, the thread fires one zero frame before the TPDO callback
    # has a chance to run (one full SYNC cycle later).
    _counts0 = [0] * num_rpdo_slots
    for nid in active_node_ids:
        cfg = NODE_CONFIG[nid]
        tk = cfg["tk_per_rev"]
        invert = -1 if cfg.get("invert_position", False) else 1
        _counts0[rpdo_slots[nid]] = int(rad2counts(float(positions[nid][0]), tk) * invert)
    first_node.rpdo[1].data = bytearray(struct.pack(f'<{num_rpdo_slots}i', *_counts0))

    # Start the periodic RPDO task before registering the callback so _task is
    # always set when populate_rpdo_data first fires.
    first_node.rpdo[1].start(period)
    first_node.tpdo[1].add_callback(populate_rpdo_data)

    # --- Main playback loop ---
    loop_count = 0

    try:
        while num_loops == 0 or loop_count < num_loops:
            loop_count += 1
            print(f"\n--- Loop {loop_count}"
                  f"{'' if num_loops == 0 else '/' + str(num_loops)} ---")

            trajectory_state["index"] = 0
            trajectory_state["start_time"] = time.time()
            cycle_complete_event.clear()
            
            # Wait for the cycle to finish (event driven by the TPDO callback)
            while not cycle_complete_event.wait(0.1):
                idx = trajectory_state["index"]
                if idx > 0 and idx <= num_points:
                    current_elapsed = time.time() - trajectory_state["start_time"]
                    first_node_tk = NODE_CONFIG[first_node_id]["tk_per_rev"]
                    cmd_deg = counts2deg(shared_pos_counts[rpdo_slots[first_node_id]], first_node_tk)
                    print(f"t={current_elapsed:6.3f} Node {first_node_id} cmd={cmd_deg:+8.2f}deg", end="\r")

            print()  # newline after \r prints

            # If we have more cycles to run, gracefully return to start and switch back to CSP mode
            if num_loops == 0 or loop_count < num_loops:
                return_to_start_position(nodes, first_node, positions)
                
                print("Switching back to CSP mode for the next cycle...")
                for node_id, node in nodes.items():
                    #node.sdo["ControlWord"].raw = 0x80
                    #node.sdo["ControlWord"].raw = 0x06
                    #node.sdo["ControlWord"].raw = 0x0F
                    node.sdo["SetModeOfOperation"].raw = 8  # CYCLIC_SYNC_POSITION
                
                first_node.rpdo[1].start(period)

    except KeyboardInterrupt:
        print("\nCtrl-C pressed, stopping...")

    finally:
        # --- Cleanup: graceful return to start position ---
        try:
            if 'first_node' in locals() and 'nodes' in locals() and 'positions' in locals():
                return_to_start_position(nodes, first_node, positions)

            # Switch all nodes to Idle Mode (0)
            print("Setting motors to idle...")
            if 'nodes' in locals():
                for node_id, node in nodes.items():
                    node.sdo["SetModeOfOperation"].raw = 0
            
            if 'network' in locals():
                network.sync.stop()

            print("Done.")

        except Exception as e:
            print(f"Cleanup error (motors may need manual reset): {e}")
        finally:
            try:
                network.disconnect()
                print("CAN bus disconnected.")
            except Exception:
                pass


if __name__ == "__main__":
    main()
