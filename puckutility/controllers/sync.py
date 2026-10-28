"""sync.py — the one owner of the CANopen SYNC producer.  No wx here.

canopen's SyncProducer.start() overwrites its task handle without stopping
the task it replaces (on socketcan a kernel BCM timer that goes on sending),
start() with no period reuses whatever period was last set, and stop() leaves
the stopped handle in place, so `_task is not None` no longer means running.
Everything that starts or stops the SYNC (the ADC monitor, the drive tests,
the calibration's measurement streams, a reboot) goes through set_sync_rate here
instead, as p4core.session.Session.set_sync_rate does for the other apps.

p4core.actuator takes this as its *sync* (plans.make_session): the same sync_rate /
set_sync_rate pair as p4gui's model, so a stream puts back the rate it found
rather than guessing it from canopen's handle.
"""

#: The ADC monitor's SYNC rate (Hz): TPDO1-3 are SYNC-triggered.
ADC_SYNC_HZ = 100


class SyncOwner:
    """Starts and stops the SYNC on the network *get_network()* returns
    (looked up on every call: the GUI replaces its network on a rescan)."""

    def __init__(self, get_network):
        self._get_network = get_network
        self._task = None
        self.sync_rate = 0

    def set_sync_rate(self, rate_hz):
        """SYNC at *rate_hz* Hz, or off for 0: every producer this owner or
        canopen started is stopped first, so there is only ever one."""
        net = self._get_network()
        sync = getattr(net, 'sync', None)
        for task in (self._task, getattr(sync, '_task', None)):
            if task is not None:
                try:
                    task.stop()
                except Exception:
                    pass    # already stopped, or the bus has gone
        self._task = None
        self.sync_rate = 0
        if sync is None:
            return
        sync._task = None
        if rate_hz > 0:
            sync.start(1.0 / rate_hz)
            self._task = sync._task
            self.sync_rate = rate_hz
