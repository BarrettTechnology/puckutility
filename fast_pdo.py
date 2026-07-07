"""FastPDO: read id/iq via continuous SYNC-driven TPDOs instead of per-sample SDO.

Proven on hardware (scripts/pdo_idiq_test.py): with network.sync.start() running continuously, TPDO4
streams a LIVE Motor.id that matches SDO exactly, and spaced SDO ops coexist with it. A single
sporadic SYNC pulse reads STALE -- continuous SYNC is required.

BUT the cal's rapid back-to-back per-settling SDO burst (mode/CW/settling re-establish) collides with
continuous SYNC (0x05040001 -- the app itself stops SYNC before SDO transactions for this reason). So
SYNC must be OFF during those bursts and ON only for the read-heavy sampling. The caller drives this:
sync_off() (default) around SDO bursts, sync_on() around a sampling burst. Validated: pause/resume
around the burst works, no orphan issue in this app.

id comes off TPDO4 (mapped to Motor.id 0x3010:6 at CONFIGURE time -- PDO mapping only changes while
not operational), iq off TPDO2's stock CurrentFeedback. read() returns [id, iq] raw (only while
sync_on()), or None -> caller falls back to SDO. Per-frame TPDO callbacks are silenced for the
duration (they'd fire every SYNC frame). `ok` is False if TPDO4 isn't carrying Motor.id (reconnect).
"""


class FastPDO:
    def __init__(self, node, tpdo_n=4, sync_period_ms=2):
        self.node = node
        self.n = tpdo_n
        self.sync_period = sync_period_ms / 1000.0
        self._t4 = None
        self._t2 = None
        self._sync_on = False
        self._cb = {}
        self.ok = False
        self.err = None

    def __enter__(self):
        n = self.node
        try:
            n.tpdo.read()
            self._t4 = n.tpdo[self.n]
            self._t2 = n.tpdo[2]
            if not any(getattr(v, 'index', None) == 0x3010 and getattr(v, 'subindex', None) == 6
                       for v in self._t4.map):
                raise RuntimeError("TPDO{} not mapped to Motor.id -- reconnect to re-run "
                                   "configure".format(self.n))
            try:
                n.network.sync.stop()                # start from a known SYNC-off state
            except Exception:
                pass
            for i in (1, 2, 3):                       # silence per-frame callbacks for the duration
                try:
                    self._cb[i] = list(n.tpdo[i].callbacks)
                    n.tpdo[i].callbacks.clear()
                except Exception:
                    pass
            self.ok = True
        except Exception as e:
            self.ok = False
            self.err = e
            self._restore_cb()
        return self

    def sync_on(self):
        """Start continuous SYNC for a sampling burst. Call AFTER the SDO control writes are done."""
        if not self.ok or self._sync_on:
            return
        try:
            self.node.network.sync.start(self.sync_period)
            self._sync_on = True
            self._t4.wait_for_reception(timeout=1.0)
        except Exception:
            self._sync_on = False

    def sync_off(self):
        """Stop SYNC before the next SDO burst (continuous SYNC + rapid SDO writes collide)."""
        if self._sync_on:
            try:
                self.node.network.sync.stop()
            except Exception:
                pass
            self._sync_on = False

    def read(self, fresh=True, timeout=0.05):
        """[id, iq] raw, or None -> caller uses SDO. Only valid between sync_on()/sync_off()."""
        if not self.ok or not self._sync_on:
            return None
        try:
            if fresh:
                self._t4.wait_for_reception(timeout=timeout)
            return [self._t4.map[0].raw, self._t2['CurrentFeedback'].raw]
        except Exception:
            return None

    def _restore_cb(self):
        for i, cbs in self._cb.items():
            try:
                self.node.tpdo[i].callbacks.extend(cbs)
            except Exception:
                pass
        self._cb = {}

    def __exit__(self, *exc):
        self.sync_off()
        self._restore_cb()
        return False
