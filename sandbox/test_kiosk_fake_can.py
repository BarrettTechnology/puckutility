"""Kiosk state-machine test against a fake canopen network (no hardware needed).

  python3 sandbox/test_kiosk_fake_can.py

Covers: no adapter -> CONNECT button, connect + auto-zero, START/STOP, auto-stop,
drive fault + recovery, over-temperature cool-down, lost CAN + reconnect.
"""
import sys, threading, time, types
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kiosk_widgets, wx
import furuta_pendulum as fp, furuta_kiosk as fk

class V:                      # .raw holder
    def __init__(s, owner, key): s.o, s.k = owner, key
    @property
    def raw(s): return s.o.read(s.k)
    @raw.setter
    def raw(s, v): s.o.write(s.k, v)
    def __getitem__(s, sub): return V(s.o, (s.k, sub))

class SDO:
    def __init__(s, node): s.n = node
    def __getitem__(s, k): return V(s, k)
    def read(s, k):
        n = s.n
        if n.net.dead: raise RuntimeError("SDO timeout")
        if k == "PositionFeedback": return n.pos
        if k == "StatusWord": return 0x0008 if n.fault else 0x0027
        if k == ("Amplifier", "Temperature"): return n.net.temp
        if isinstance(k, tuple) and k[0] == 0x1601: return n.map.get(k[1], 0)
        raise KeyError(k)
    def write(s, k, v):
        if s.n.net.dead: raise RuntimeError("SDO timeout")
        if isinstance(k, tuple) and k[0] == 0x1601: s.n.map[k[1]] = v
        if k == "ControlWord": s.n.cw.append(v); s.n.fault = s.n.fault and v != fp.CLEAR_FAULT

class PDO(dict):
    def __init__(s, node): super().__init__(); s.node=node; s.cob_id=None; s.cbs=[]
    def add_callback(s, cb): s.cbs.append(cb)
    def transmit(s): pass
    def __getitem__(s, k):
        if k == 'PositionFeedback': return types.SimpleNamespace(raw=s.node.pos)
        return s.setdefault(k, types.SimpleNamespace(raw=0))
class PDOs(dict):
    def __init__(s, node): super().__init__({1: PDO(node), 2: PDO(node)})
    def read(s): pass

class Node:
    def __init__(s, net, nid):
        s.net, s.id, s.pos, s.fault = net, nid, 1000 * nid, False
        s.map = {0: 2, 1: 0x607A0020, 2: 0x60FF0020}; s.cw = []
        s.nmt = types.SimpleNamespace(state=None); s.sdo = SDO(s)
        s.tpdo, s.rpdo = PDOs(s), PDOs(s)

class Sync:
    def __init__(s, net): s.net, s.tasks = net, []
    def start(s, period):
        ev = threading.Event(); s.tasks.append(ev)
        def run():
            while not ev.is_set():
                if not s.net.dead:
                    for n in s.net.nodes.values():
                        for cb in n.tpdo[1].cbs: cb(None)
                time.sleep(period)
        threading.Thread(target=run, daemon=True).start()
    def stop(s):
        for ev in s.tasks: ev.set()
        s.tasks = []

WORLD = types.SimpleNamespace(present=False, temp=40, nets=[])
class Net:
    def __init__(s):
        s.nodes, s.sent, s.dead, s.temp = {}, [], False, WORLD.temp
        s.sync = Sync(s); s.scanner = types.SimpleNamespace(reset=lambda: None, search=lambda: None,
                                                          nodes=[1, 2]); WORLD.nets.append(s)
    def connect(s, **kw):
        if not WORLD.present: raise OSError("No such device can0")
    def add_node(s, nid, eds): s.nodes[nid] = Node(s, nid); return s.nodes[nid]
    def send_message(s, cob, data):
        if s.dead: raise OSError("bus down")
        s.sent.append((cob, bytes(data)))
    def disconnect(s): s.sync.stop()

fp.canopen = types.SimpleNamespace(Network=Net)
fk.RETRY_S = 0.3
fk.FurutaKioskFrame._pick_port = staticmethod(lambda: 'can0')


app = wx.App()
k = fk.FurutaKioskFrame()
net = lambda: WORLD.nets[-1]
results = []
def check(name, cond):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name, "| state =", k._state, flush=True)

def wait(pred, t=6.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        wx.SafeYield(); time.sleep(0.02)
        if pred(): return True
    return False

def script():
    k._start_connect_worker()
    check("no adapter -> CONNECT button shown", wait(lambda: k._btn_connect.IsShown()))
    WORLD.present = True
    check("adapter appears -> connects, zeroes, READY", wait(lambda: k._state == fk.READY))
    check("CONNECT button hidden once connected", not k._btn_connect.IsShown())
    k._on_main_button()
    check("START -> RUNNING", wait(lambda: k._state == fk.RUNNING))
    n1 = net().nodes[1]
    check("RPDO2 remapped to TargetTorque", n1.map.get(1) == 0x60710010 and n1.map.get(0) == 1)
    check("control loop sending torque", wait(lambda: len([m for m in net().sent if m[0] == 0x301]) > 50))
    check("exactly one SYNC task", len(net().sync.tasks) == 1)
    n1.fault = True
    check("drive fault -> FAULT / Stopped", wait(lambda: k._state == fk.FAULT))
    check("loop stopped on fault", not k._controlling)
    k._on_main_button()
    check("START after fault -> clears fault, RUNNING", wait(lambda: k._state == fk.RUNNING) and not n1.fault)
    check("RPDO2 backup is still the ORIGINAL mapping", k._rpdo2_backup == {0: 2, 1: 0x607A0020, 2: 0x60FF0020})
    check("still exactly one SYNC task", len(net().sync.tasks) == 1)
    k._on_main_button()
    check("STOP -> PARKING (bring it down + home)", wait(lambda: k._state == fk.PARKING, 2))
    check("park finishes -> READY", wait(lambda: k._state == fk.READY, 8))
    fk.AUTO_STOP_S = 1.0
    k._on_main_button()
    check("START again -> RUNNING", wait(lambda: k._state == fk.RUNNING))
    check("auto-stop after AUTO_STOP_S -> park -> READY", wait(lambda: k._state == fk.READY, 10)
          and "automatically" in k._status.GetLabel())
    check("auto-stop leaves the arm limp (zero torque)",
          [m for m in net().sent if m[0] == 0x301][-1][1] == b'\x00\x00')
    fk.AUTO_STOP_S = 0
    k._on_main_button(); wait(lambda: k._state == fk.RUNNING)
    time.sleep(1.5); wait(lambda: False, 0.3)
    check("--auto-stop 0 -> never stops on its own", k._state == fk.RUNNING)
    k._on_main_button(); wait(lambda: k._state == fk.PARKING, 2)
    k._on_main_button()                                  # STOP again during the park
    check("STOP during park -> limp immediately -> READY", wait(lambda: k._state == fk.READY, 2)
          and not k._parking)
    check("last torque sent was zero", [m for m in net().sent if m[0] == 0x301][-1][1] == b'\x00\x00')
    net().temp = 90
    check("over-temp -> COOLING", wait(lambda: k._state == fk.COOLING))
    check("START disabled while cooling", not k._btn_main._enabled)
    check("RPDO2 restored after over-temp disable", n1.map.get(1) == 0x607A0020 and n1.map.get(0) == 2)
    net().temp = 60
    check("cooled -> READY", wait(lambda: k._state == fk.READY))
    k._on_main_button(); wait(lambda: k._state == fk.RUNNING)
    old = net(); old.dead = True
    check("CAN lost while running -> Reconnecting", wait(lambda: k._state == fk.CONNECTING))
    check("new connection -> READY again", wait(lambda: k._state == fk.READY and net() is not old, 10))
    k._shutdown_hw()
    print(f"\n{sum(r[1] for r in results)}/{len(results)} passed", flush=True)
    wx.CallAfter(app.ExitMainLoop)
    global FAILED; FAILED = not all(r[1] for r in results)

FAILED = True
wx.CallAfter(script)
app.MainLoop()
sys.exit(1 if FAILED else 0)
