# Barrett — Multi-Motor Trajectory Playback

This application streams synchronous joint trajectory data from a JSONL file to multiple CANopen motor controllers over a single CAN FD bus. It utilizes the CiA 402 motor control profile to cleanly transition between operating modes.

## How It Works

This script leverages an event-driven architecture using `python-canopen` to achieve highly deterministic multi-axis synchronization without relying on standard Python blocking `sleep()` loops.

1. **Initialization:** The script loads a JSONL trajectory file and calculates the base transmission frequency (e.g., 50 Hz) directly from the timestamps in the file.
2. **Network Setup:** It connects to the specified CAN FD bus and interrogates the bus to find the configured nodes.
3. **Configuration:** 
   - **RPDO1 (Dynamic Size CAN FD):** Configured for COB-ID `0x200`. It maps one 32-bit `TargetPosition` object for every active node detected on the bus. Nodes are configured to listen to their specific 4-byte slice of the payload based on their discovery order, using standard `0x0007` dummy padding for the other slots.
   - **TPDO1:** Configured to transmit `StatusWord`, modes, positions, velocity, current, and temperature synchronously.
   - Motors are initialized and placed into **Cyclic Synchronous Position (CSP)** mode.
4. **Playback (Event-Driven):** 
   - A background network thread broadcasts CAN SYNC messages at the determined rate.
   - When the first motor responds with its TPDO, a callback is triggered. This callback immediately computes the next set of target positions for the trajectory and packs them into the RPDO data buffer.
   - Because the RPDO is set to synchronous transmission, the `canopen` background thread fires the payload onto the bus immediately on the next SYNC, guaranteeing precise 50 Hz alignment.
5. **Graceful Reset:** At the end of the trajectory (or if interrupted), the script gracefully switches all motors from CSP mode to **Profile Position Mode**. It safely moves all motors back to their initial trajectory positions, continuously polling their `StatusWord` via incoming TPDOs until the "Target Reached" bit goes high. It then switches the motors to Idle or restarts the cycle.

---

## Installation

### 1. Python Environment
```
setup-venv.sh
source bin/activate
setup-pip.sh
```

---

### 2. Linux Setup (SocketCAN)

Linux relies on standard SocketCAN drivers. Ensure your CAN FD adapter is plugged in and brought up correctly.

**Bring up the CAN interface (Example using 1Mbps nominal / 5Mbps data):**
```bash
sudo ip link set can0 type can bitrate 1000000 dbitrate 5000000 fd on
sudo ip link set up can0
```

*Note: If you encounter issues with interface states, you can use the provided `./can_reset.sh` (if available in your repository) to cleanly reset the bus.*

---

### 3. Windows Setup (CandlelightBus)

Running on Windows requires a CANable 2.5 (or compatible) adapter flashed with Candlelight firmware.

**Prerequisites:**
1. Install **WinUSB** drivers for the CANable device (you can use a tool like [Zadig](https://zadig.akeo.ie/)).
2. You must have `libusb-1.0.dll` installed. Place it in the root directory of this project or ensure it is accessible on your system `PATH`.
3. Install the Windows-specific dependencies:
   ```bash
   pip install pyusb
   ```
*(The custom `candlelight_bus.py` driver included in this directory handles the interface bridging).*

---

## Configuration

Motor parameters, gearbox ratios, PD tuning, and the mapping of Node IDs to JSONL joint labels are all managed in the `node_config.py` file. 

Edit `node_config.py` to add or modify your motor configurations:

```python
# node_config.py
NODE_CONFIG = {
    1: {
        "joint_name": "flj1",
        "invert_position": False,
        "tk_per_rev": 4096 * ((1 + 55 / 17) ** 2),  # Example 18:1 gearbox
        "Position_Kp": 30,
        "Velocity_Kp": 0.0025,
        "Velocity_Ki": 0.0,
        "Velocity_Filter": 50,
    },
    # Add nodes here...
}
```

---

## Running the Application

Execute `trajectory.py` from the command line, passing the CAN interface, the number of cycles to loop, and the trajectory file.

**Format:**
```bash
python trajectory.py <can_channel> <num_cycles> <jsonl_file>
```
- `<can_channel>`: The CAN interface to use (e.g., `can0` on Linux. On Windows, passing `can0` parses out channel `0` for CandlelightBus).
- `<num_cycles>`: The number of times to loop the playback. Set to `0` for infinite loops.
- `<jsonl_file>`: The path to the trajectory data file.

**Example (Run 5 cycles on can0):**
```bash
python trajectory.py can0 5 sin.jsonl
```

**Example (Infinite loop on can1):**
```bash
python trajectory.py can1 0 sin.jsonl
```
