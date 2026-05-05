import can
import canopen
from canopen.network import PeriodicMessageTask
from canopen.sync import SyncProducer
import platform
import time
from typing import Optional
import queue


class MyPeriodicMessageTask(PeriodicMessageTask):
    """
    Task object to transmit a message periodically using python-can's
    CyclicSendTask
    """

    def __init__(
        self,
        can_id: int,
        data: bytes,
        period: float,
        bus,
        remote: bool = False,
        callback=None,
    ):
        self.callback = callback
        self.bus = bus
        self.period = period
        self.msg = can.Message(is_extended_id=can_id > 0x7FF,
                               arbitration_id=can_id,
                               data=data, is_remote_frame=remote,
                               is_fd=True, bitrate_switch=True)
        self._task = None
        self._start()

    def _start(self):
        # Always pass a modifier_callback to force thread-based cyclic task.
        # BCM-based tasks (used when modifier_callback is None) do not correctly
        # update CAN FD frame data on modify_data() calls, causing stale zero data.
        self._task = self.bus.send_periodic(
            self.msg, self.period,
            modifier_callback=self.callback if self.callback is not None else lambda msg: None
        )

    def update(self, data: bytes):
        """Update data of a periodic message."""
        self.msg.data = data
        if self._task is not None:
            self._task.modify_data(self.msg)


class MySyncProducer(SyncProducer):
    def __init__(self, network, callback=None):
        self.callback = callback
        super().__init__(network)

    def start(self, period: Optional[float] = None):
        self._task = MyPeriodicMessageTask(self.cob_id, [], period, self.network.bus, callback=self.callback)


class SyncMessageCallback:
    def __init__(self, queue_to_use):
        self.queue = queue_to_use

    def __call__(self, msg):
        if self.queue is not None:
            self.queue.put({"SYNC": True})


class RealNetwork(canopen.Network):
    """ CAN FD Network using SocketCAN (Linux) or CandlelightBus (Windows) """

    def send_periodic(self, can_id, data, period, remote=False):
        """Override to use CAN FD periodic task."""
        return MyPeriodicMessageTask(can_id, data, period, self.bus, remote)

    def __init__(self, raw_data_q: queue.Queue, fd: bool = True, can_channel: str = "can0"):
        super().__init__()  # Initialize the parent canopen.Network class

        # Overload network.sync to support call-back function
        sync_callback = SyncMessageCallback(raw_data_q)
        self.sync = MySyncProducer(self, callback=sync_callback)

        if platform.system() == "Windows":
            channel_num = int(can_channel[3]) if len(can_channel) > 3 and can_channel[3].isdigit() else 0
            print(f"*** Connecting to CAN FD network on Windows using CandlelightBus (channel {channel_num}) ***")
            from candlelight_bus import CandlelightBus
            self.bus = CandlelightBus(
                channel=channel_num, 
                bitrate=1_000_000, 
                fd=True,
                data_bitrate=5_000_000, 
                sample_point=0.75, 
                data_sample_point=0.75
            )
        elif platform.system() == "Linux":
            print(f"*** Connecting to CAN FD network on Linux using SocketCAN ({can_channel}) ***")
            self.connect(
                interface="socketcan",
                channel=can_channel,
                fd=True,
                bitrate=1_000_000,
                data_bitrate=5_000_000
            )
        else:
            raise RuntimeError("Unsupported platform. Please use Windows or Linux.")

        # Reset the network
        self.scanner.reset()

        # Search for nodes
        self.scanner.search()
        time.sleep(0.5)
        print("Detected CAN IDs on the network:", self.scanner.nodes)
        # for node_id in network.scanner.nodes:
        #     print(f"Node ID: {node_id}")``
