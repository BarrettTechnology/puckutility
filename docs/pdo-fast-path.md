# PDO fast-path rebuild — the read "freeze" was the drive collapsing, not reception

Bidirectional fast path: fast READ (id/iq via TPDO) and fast WRITE (Theta_e/ud via RPDO). The
hardware diagnostic settled the mystery: READ and WRITE share ONE root cause.

## 1. Confirmed root cause (from `scripts/pdo_coherent_test.py` on hardware)

- **PHASE 4 WRITE = PASS.** Streaming Theta_e/ud via RPDO3 makes current that rotates with the
  commanded angle. The earlier "didn't spin" was an **unprimed RPDO1**.
- **PHASE 2 = PASS.** SYNC reaches the puck via `network.sync.start()`; frames arrive every SYNC.
- **PHASE 3 READ was NOT a reception bug.** Frames arrived every level, `wait_for_reception`'s
  timestamp advanced, and **RAWCAP == PDOMAP and both TRACKED** the current. They were never frozen.
  The problem: SDO `|I|` during the read burst was only ~10–30 mA and didn't rise with `ud` (itiming
  gets ~300 mA at ud≈9800). **There was almost no current to read.**

**Why:** turning SYNC on to stream the TPDO makes the firmware re-apply the **unprimed RPDO1** buffer
(`ControlWord=0`, Disable Voltage) on **every SYNC**, disabling the drive during the very read burst.
This is the *same* RPDO1 issue PHASE 4 fixed. **READ and WRITE are coupled:** to read a SYNC-driven
TPDO with real current you must keep the drive enabled during SYNC. This also explains the earlier
"frozen" reads inside the itiming/slope cals — the drive was collapsing on each `sync_on()` burst.

(The RPDO1 mechanism itself is documented in `calibrate_menu.py:37-45, 245-283`: RPDO1 at
`trans_type=0` is applied every SYNC; its startup buffer holds ControlWord=0.)

## 2. The unified fix — FastPDO keeps the drive enabled during its SYNC read

`FastPDO` now folds the RPDO1 arm/prime into its own lifecycle (`keep_drive_enabled=True` default):

- `__enter__` — in the same PRE-OPERATIONAL window that remaps the TPDO, set `RPDO1 trans_type = 255`
  (async: applied on receipt only, never on SYNC). Saves the original `0x1400:02` to restore.
- `sync_on()` — **prime RPDO1 once** with `send_message(0x200+id, struct.pack('<HBh', OP_ENABLED,
  mode, 0))` before starting SYNC, so the drive stays OP_ENABLED and SYNC can no longer disable it.
- `__exit__` — restore the original RPDO1 `trans_type` (and the original TPDO4 map), unsubscribe.

The caller keeps driving `ud` via SDO as before; FastPDO only guarantees the drive isn't torn down by
SYNC. `mode` (default `PHASE_VOLTAGE_ANGLE`=12) and `keep_drive_enabled` are constructor args; set
`keep_drive_enabled=False` for callers that manage RPDO1 themselves.

### Read mechanism (kept from the prior step, still the robust path)
Both signals are mapped into ONE TPDO (coherent: one frame = one SYNC instant) and captured via a
**raw** `network.subscribe(cob, cb)` callback that `struct.unpack`s the bytes in the notifier thread
(the proven `_PVCATorqueDialog` reception path) — not PdoMap `.raw`. `read()` gates on a frame counter
for freshness and returns `[id, iq]`. On hardware RAWCAP and PDOMAP agreed, so this is belt-and-braces
rather than strictly required, but it removes the main-thread `.raw`/`wait_for_reception` fragility for
good and is the mechanism this app is proven to deliver.

canopen citations: dispatch `network.py:236-253`; `PdoMap.on_message`/`.data` `pdo/base.py:300-311`;
`.raw` slicing `pdo/base.py:565-585`; `wait_for_reception` `pdo/base.py:537-546`;
`add_variable(index, subindex, bits)` `pdo/base.py:456-491`.

### Mapping bytes/widths (from `puck4.eds`)
| pair | slot 0 | slot 1 | frame |
|------|--------|--------|-------|
| `idiq` (default) | Motor.id 0x3010:6 INT16 | CurrentFeedback 0x6078 INT16 | `<hh`, 4 B |
| `alphabeta` | Alpha.Filtered 0x3008:2 INT32 | Beta.Filtered 0x3009:2 INT32 | `<ii`, 8 B |

Map words (`index<<16 | subindex<<8 | length`): `idiq` → `0x30100610`, `0x60780010`. RPDO3 (in
`RPDOWriter`): Theta_e `0x60EA0010`, Motor.ud `0x30100410`.

## 3. WRITE helper (`RPDOWriter`) — confirmed PASS

Standalone opt-in context manager mirroring `_PVCATorqueDialog`: RPDO1 async + RPDO3 map (Theta_e +
Motor.ud, trans_type=255) + prime RPDO1 OP_ENABLED; `write(theta_e, ud)` = one `send_message` applied
on receipt (no SDO, no ACK). Idles the drive on `__exit__`. PHASE 4 verified current flows and rotates
with Theta_e. Kept separate so `calibrate_menu.py` can adopt it for spin/high-rate streaming without
disturbing SDO flows.

## 4. Public API
`FastPDO`: `__enter__`/`__exit__`, `sync_on()`, `sync_off()`, `read()`→`[id, iq]` or `None`, `.ok`,
`.err`. New optional ctor args: `mode`, `keep_drive_enabled`, plus `tpdo_n`, `sync_period_ms`, `pair`.
Existing `FastPDO(self.node)` calls in `calibrate_menu.py` are unchanged and now automatically keep the
drive enabled during their read bursts. New: `RPDOWriter(node, mode=12)` — `.ok`/`.err`,
`write(theta_e, ud)`, context-managed.

## 5. Hardware validation — one command
```
python3 scripts/pdo_coherent_test.py can0 127
```
Single safe run (gentle current; drive idled + TPDO4/RPDO1 restored on exit/Ctrl-C):

- **PHASE 1** config read-back.
- **PHASE 2** SYNC-mechanism A/B (frames on TPDO4 COB-ID: sync.start vs manual thread).
- **PHASE 3** (×2, callbacks intact/cleared) — now **arms RPDO1 async + primes OP_ENABLED before each
  SYNC** and ramps `ud` to ~10000 for a REAL current. Per level: `|I|` via RAWCAP, PDOMAP, SDO, plus
  frames delta, raw last-bytes, and `wait_for_reception` timestamp advance.
- **PHASE 3c** drives `FastPDO.read()` exactly as `calibrate_menu` does (ramp to ud≈10000).
- **PHASE 4** RPDO write via `RPDOWriter`.

### Corrected PASS criteria (PHASE 3 / 3c)
- **PASS** = the drive **held real current** (SDO `|I|` span ≥ `REAL_SPAN`=100 raw counts across the
  ramp) **AND** RAWCAP / FastPDO `|I|` **track SDO** at every level (within `max(30, 25%)`). If PDOMAP
  stays frozen while RAWCAP tracks, the raw-capture path is doing the work — still PASS.
- **INCONCLUSIVE** = SDO `|I|` span < 100 → the drive didn't hold current under SYNC (RPDO1 coupling
  not taking); fix that before trusting any read numbers. (This is exactly what the *previous* run hit.)
- **FAIL** = current swept but RAWCAP frozen on the wire → puck transmits stale 2-var TPDO4 → fall back
  to split single-var TPDOs (each `trans_type=1`), which `FastPDO`'s `pair`/`tpdo_n` args make small.

Each probe prints PASS/FAIL/INCONCLUSIVE with counts and a one-line diagnosis.

## 6. Continuous-spin primitive (`SpinScanner`) — the shared fast primitive for rotor-parking cals

Rotor-parking cals (slope now; itiming / gain / enczero later) sweep the electrical cycle, and the
per-angle **stop + settle** dominates their runtime. `SpinScanner` removes the settles.

### Phase-4 reinterpretation (the write was already applying)
In `PHASE_VOLTAGE_ANGLE` with `uq=0`, commanding `Theta_e` takes the firmware D-axis-stall branch
(app `pwm.c` ~518): the rotor magnetically **detents to and follows** the commanded angle. So the
**rotor-frame** id/iq stay ~d-axis (id≈const, iq≈0) *even when the write is applied correctly* — which
is exactly the Phase-4 data (id 133→158, iq 0→41). The write works; the old check just looked at the
wrong frame. The **stator** current (Alpha/Beta.Filtered) rotates with `Theta_e` and traces the
current **circle**.

### Design
`SpinScanner` composes the validated read path — `FastPDO(pair='alphabeta', keep_drive_enabled=True)`
(alpha/beta over ONE TPDO, RPDO1 async-prime, SYNC) — and adds an RPDO3 map (`Theta_e` + `Motor.ud`,
async). `spin(ud, cycles, rate_hz, settle_s, dt)` detents at θ=0, then ramps `Theta_e` through
`cycles` electrical cycles at `rate_hz` cycles/s via RPDO3 while capturing `(theta_cmd, alpha, beta)`
every `dt` — **no per-angle SDO settle**. `scan_offset(ud, alpha_bias, a_sens, beta_bias, b_sens,
**spin_kw)` converts counts→units, fits the circle, and returns **`(offset_alpha, offset_beta, meanI)`
= (center_x, center_y, radius)** — center = sense offset, radius = |I| — the same quantity the stepped
slope cal's `_sweep_level`/`_circle_fit` produce (Kasa fit, reimplemented pure-Python here as
`kasa_circle_fit`; verified to recover a known center to <1% and even from a partial arc, which
matters under rotor lag). `self.last_residual` (`circle_rms_residual`, fraction of R) gauges circle
quality. Idles the drive + restores TPDO4/RPDO1/RPDO3 on `__exit__`.

### Theta_e scaling / units
`0x60EA` is INT16; **65536 counts = one full electrical cycle** (matches the slope cal's
`r = theta/32768*pi`). `spin()` runs a free 0..65535 counter folded to signed-16 by `_wrap16`.
PHASE 1 of the spin test verifies the `(alpha,beta)` vector sweeps exactly 2π about the fitted center
over one `Theta_e` cycle (confirms scaling *and* that the RPDO write is applied).

### Sweep-rate limit
The field must rotate slowly enough that the detenting rotor + current (bounded by R/L and rotor
inertia/friction) keep up. Too fast → the magnitude droops/lags and the circle degrades (residual
rises, center drifts). Default `rate_hz=0.5` (one electrical cycle / 2 s), comparable to the stepped
cal's per-angle rate. The spin test's PHASE 2 sweeps `rate_hz ∈ {0.25, 0.5, 1.0, 2.0}` and prints the
residual per rate so the fastest safe rate is picked from data; the Kasa center stays valid as long as
the points span a reasonable arc (it's an algebraic fit), so degradation is graceful.

### API for a cal
```python
with SpinScanner(node) as s:            # sets up alpha/beta read + RPDO3 write + keeps drive enabled
    if s.ok:
        for ud in ud_levels:            # one continuous spin per current level, no settles
            off_a, off_b, meanI = s.scan_offset(ud, alpha_bias=ab, a_sens=asens,
                                                 beta_bias=bb, b_sens=bsens, rate_hz=0.5)
            # fit off_a/off_b vs meanI across levels -> kA,kB slope (same as the stepped cal)
```

### Validation — `scripts/pdo_spin_test.py`
```
python3 scripts/pdo_spin_test.py can0 127
```
- **PHASE 1** field rotates + Theta_e scaling: alpha/beta sweeps ~2π about the fitted center over one
  cycle and the circle is clean → PASS (proves the write applies in the stator frame).
- **PHASE 2** sweep-rate characterization: residual per `rate_hz` → the fastest safe rate.
- **PHASE 3** stepped (SDO) vs continuous (spin) offset at one `ud`: PASS if centers agree (within
  ~10% of |I|) and the continuous run is much faster (reports the speedup).

## 7. Files
- `fast_pdo.py` — `FastPDO` unified (raw-capture read **+ RPDO1 arm/prime to hold the drive enabled
  during SYNC**); `RPDOWriter` write helper; **`SpinScanner` continuous-spin primitive** +
  `kasa_circle_fit` / `circle_rms_residual` helpers.
- `scripts/pdo_coherent_test.py` — one-run diagnostic; PHASE 3 establishes real current; PHASE 4
  reinterpreted (id/iq staying put is expected).
- `scripts/pdo_spin_test.py` — continuous-spin validation (rotation/scaling, rate sweep, stepped-vs-
  continuous equivalence).
