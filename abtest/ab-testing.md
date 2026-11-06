# A/B testing P4 firmware 4.3.5 against 5.0.5

This guide is written for whoever runs the test, human or assistant. Every
command in it was run on the bench (two actuators on one bus, 42 V) while it
was written, and the output shown is that run's output. Where your output
differs in more than the numbers, stop and find out why before going on.

What the package compares: one actuator, calibrated the same way on each
firmware, driving the same 50 Hz cyclic-synchronous-position (CSP)
trajectories, captured through the same PDOs. The two firmwares get their
own configuration CSV each, tuned natively on that firmware.

Contents
1. Setup
2. The command line, command by command
3. Calibrating an actuator from the command line
4. The A/B procedure
5. The 5.x data logger
6. Collecting data on 4.x
7. CANopen changes 4.3.5 -> 5.x
8. Velocity estimation
9. SYNC rate and 0x60C2
10. Analysis recipe
11. Troubleshooting

---

## 1. Setup

### 1.1 Install

Install the tools as described in `install.md` (Python wheels for the CLI,
the installer for the CAN support). You need two things on the
PATH or importable:
- the `puckutility` command (the CLI, sections 2 and 3);
- Python 3.9+ with `p4core`, `numpy` and `matplotlib` importable, for
  `abtest/ab_testing.py`. It runs its stages as `python -m p4core ...` with
  the same interpreter, so p4core must be importable from that one.

Check:
```
$ puckutility --version
$ python -c "import p4core, numpy, matplotlib"
```

### 1.2 The CAN adapter

`can-adapters.md` covers the adapters (PEAK PCAN-USB, the dev board's
CANable) and their drivers. This test uses **classic CAN at 1 Mbit/s** on both
firmwares. On Linux:
```
$ sudo ip link set can0 down
$ sudo ip link set can0 type can bitrate 1000000 restart-ms 100
$ sudo ip link set can0 up
$ ip -d link show can0          # check: "bitrate 1000000", state ERROR-ACTIVE
```
A CANable rejects `restart-ms`; leave it out there. On Windows, `--can 0` is
the first CandleLight (or PCAN) adapter.

Every command takes `--can PORT`. Set `P4_CAN` once instead of repeating it:
```
$ export P4_CAN=can0
```
The examples below spell out `--can can3`, the bench's port. Use yours.

### 1.3 Safety, before anything moves

- **The output must be free to turn.** Calibration turns the motor one
  revolution each way; the A/B profiles move the output by up to tens of
  degrees at 90 deg/s. Nothing may be attached that cannot take that.
- **Supply:** 42 V, with the current limit set. Both actuators drew under
  2 A average on the bench; a limit of about 5 A catches a fault without
  browning the bus out on a step.
- **Never enable a drive downgraded to 4.3.5 before wipe, CSV load and
  calibration have run.** 5.x stores the current-sense bias in Q12.4 (x16);
  4.3.5 reads the same NV word as whole counts, about 32000 instead of 2000,
  and the current loop then sees amps of offset that are not there. The
  `prepare` command (section 4) always does wipe, load and calibrate in that
  order, and `run` refuses a drive whose bias or gain factor is implausible.
  Upgrading 4.3.5 -> 5.x is safe: 5.x migrates the bias at boot.
- Only address nodes by `--id`. Never use `--all` on a bus that carries
  nodes outside the test.

### 1.4 Identify the bus

Read only; these commands change nothing.
```
$ puckutility --can can3 scan
Scanning the bus...
Found 2 node(s): [4, 13]
4 13
$ puckutility --can can3 --id 13 version
4.3.5 n/a
$ puckutility --can can3 --id 13 info --no-flashloader
Node 13
  Firmware:     4.3.5
  Githash:      n/a
  Model:        P4-32  (product code 5760)
  Serial:       1002
  PWM:          80.0 kHz
  Bus voltage:  40.9 V
  Temperature:  37 C
  ADC settling: 500 ns
  Enc comp:     not in this firmware
```
Record the node IDs, models and serials. `info` without
`--no-flashloader` also reports the flashloader version (section 2.8), at
the cost of a reboot into the flashloader and back.

The bench actuators, for reference:

| Actuator | Board | Gear (0x6091) | Node on the bench |
|---|---|---|---|
| BO4523 HFM | P4-42 | 26.4:1 (264/10) | 4 |
| BO3518 | P4-32 | 60.84:1 (60840/1000) | 13 |

---

## 2. The command line, command by command

All commands share these options: `--can PORT`, `--id N`, `--fd`/`--classic`,
`-y` (answer yes to every question), `-v`/`-q` (more/less output), `--json`
(machine-readable result on stdout; progress text still prints).

Exit codes: `0` success, `1` the operation failed, `2` usage error,
`3` no device answered, `130` interrupted (Ctrl-C).

### 2.1 Enumerate nodes: `scan`
```
$ puckutility --can can3 scan
Scanning the bus...
Found 2 node(s): [4, 13]
4 13
$ puckutility --can can3 --json scan
Scanning the bus...
Found 2 node(s): [4, 13]
[
  4,
  13
]
```
The last line of the plain form is the IDs alone, for scripts.

### 2.2 Firmware version and githash: `version`, `info`
```
$ puckutility --can can3 --id 13 version
4.3.5 n/a
$ puckutility --can can3 --id 13 --json version
{
  "firmware": "4.3.5",
  "githash": null,
  "node_id": 13
}
```
`version` prints `VERSION GITHASH`. 4.3.5 has no githash object (0x3003), so
it prints `n/a` (`null` in JSON). On 5.x the githash is 0x3003; a trailing
`+` marks a build from a dirty tree. 0x100A is a U32 on 4.3.5 and a string
on 5.x; the CLI reads both.

`info` adds the board identity and live values:
```
$ puckutility --can can3 --id 13 --json info --no-flashloader
{
  "bus_voltage_v": 41.0,
  "encoder_comp": null,
  "firmware": "4.3.5",
  "firmware_line": "4.3.x",
  "flashloader": null,
  "githash": null,
  "model": "P4-32",
  "node_id": 13,
  "product_code": 5760,
  "serial": 1002,
  "settling_ns": 500,
  "temperature_c": 37
}
```

### 2.3 Change the node ID: `set-id NEW`

Writes the new ID, saves it, resets the node, waits for it at the new ID,
then moves the eight PDO COB-IDs to the new ID and saves those too. No
separate reboot is needed.
```
$ puckutility --can can3 --id 13 -y set-id 20
Setting node ID 13 -> 20...
Node 13 is now node 20; PDO COB-IDs remapped and saved
$ puckutility --can can3 scan
Scanning the bus...
Found 2 node(s): [4, 20]
4 20
```
(It was set back with `--id 20 -y set-id 13`.) Pick an ID no other node
uses; `scan` first.

### 2.4 Save NV to JSON: `nv save FILE`
```
$ puckutility --can can3 --id 13 nv save node13.json
Reading node 13's NV...
275 NV value(s) -> node13.json
```
Every non-volatile entry of the **running** firmware's object dictionary,
as raw values, with the firmware version, githash, node ID and date in the
header. 4.3.5 has 275 NV entries, 5.0.5 322. Take one before anything
writes to a node: it is your restore point.

### 2.5 Restore NV from JSON: `nv restore FILE`
```
$ puckutility --can can3 --id 13 -y nv restore node13.json
Write node13.json to node 13's NV and reboot it? [yes: --yes]
Writing 275 NV value(s)...
Saved; rebooting...
269 written, 0 refused, 0 differ after the reboot
```
The identity entries (0x1018,x, serial 0x3005,1/2) and the node ID (0x21B0)
are never written, which is the difference between 275 and 269. A file
saved on a different firmware version is refused unless `--force`: NV
layouts and units differ between 4.3.5 and 5.x (section 7). To move a node
between firmwares, use `prepare` (section 4), not `nv restore`.

### 2.6 Erase NV: `wipe-nvm`
```
$ puckutility --can can3 --id 4 -y wipe-nvm --no-template --backup prewipe.csv
Erase node 4's NVM? Only communication and identity, the node ID and the CAN termination stay; motor data and board calibration go. [yes: --yes]
Backing node 4 up to prewipe.json...
Backup: prewipe.json
Erasing NVM (4.3.x: writing the defaults)...
Writing 98 NV row(s) to their 4.3.5 defaults...
Saved; rebooting...
3001,1 is not at its default after the reboot (the firmware corrects some at boot: PWM, poles)
3011,3 is not at its default after the reboot (the firmware corrects some at boot: PWM, poles)
98 written, 0 refused, 2 not at the default after the reboot
WARNING: Gainfactor is 0: the drive cannot be enabled until the CSV is loaded and bias/gain are calibrated
WARNING: No template loaded: load the configuration CSV next; the drive cannot be enabled until it is
```
That is 4.3.5, which has no erase command: the CLI writes every NV entry's
EDS default, saves and reboots. 5.x erases with its own command (0x1011,5)
and prints `Erasing NVM...`. The two warnings are expected: the CSV and the
calibration come next. `--no-template` skips loading a blank board template
(the A/B loads its own CSV). `--backup` is written as an NV JSON when no
template matches the board, as here.

### 2.7 Write a configuration CSV: `config load CSV`
```
$ puckutility --can can3 --id 4 -y config load P4-42-42V-20A-BO4523-HFM-fw4.3.5-v1.0.0-2026-11-06.csv
Rebooting...
Loading P4-42-42V-20A-BO4523-HFM-fw4.3.5-v1.0.0-2026-11-06.csv into node 4...
File information: Configure Puck 4
File version: V1.0.0 (2026-11-06)
line 11: NMT 0x0101 skipped (Reset node)
90 value(s) written, 0 error(s)
Saving to NVM...
Rebooting...
```
The CSV writes, saves all and reboots. `config check CSV` validates a file
against the EDS without a node. The 4.3.5 CSVs have 90 rows, the 5.0.5 CSVs
108. Use the CSV for the firmware the node runs: the file name says which
(`fw4.3.5` / `fw5.0.5`). A row the firmware does not know is an error, not a
warning.

### 2.8 Update firmware: `flash FILE`
```
$ puckutility --can can3 --id 4 -y flash puck4.app.stm32.release.v4.3.5-0-g6605fba.bin
Rebooting node 4 into its flashloader...
Node 4: flashloader 3.0.7
Erasing...
Programming puck4.app.stm32.release.v4.3.5-0-g6605fba.bin (55704 bytes)...
Application validated. Launching...
Node 4 running 4.3.5
```
Flashloader 3.0.x takes the plain `.bin`; 3.1.0 and later take only the
encrypted `.ebin`. `info` (without `--no-flashloader`) tells you which you
have. Flashing does not touch NV, which is why a downgrade must be followed
by a wipe (section 1.3).

### 2.9 Calibrate: `calibrate [STEP ...]`

Section 3 explains the steps. Run them as a group or one at a time:
```
$ puckutility --can can3 --id 13 -y calibrate ab                 # all four, in order
$ puckutility --can can3 --id 13 -y calibrate offsets            # current offsets
$ puckutility --can can3 --id 13 -y calibrate gainfactor         # current gain factor
$ puckutility --can can3 --id 13 -y calibrate encoder-offset     # encoder offset, e-zero, winding order
```
`ezero` is another name for `encoder-offset` (section 3.1). Each step saves to
NV as it passes.

One step, plain and JSON:
```
$ puckutility --can can3 --id 13 -y calibrate offsets
--- Node 13 ---
other nodes set PRE-OPERATIONAL (no PDOs while node 13 is calibrated)
=== Current-sense bias (1/1) ===
Calibrating current-sense bias...
Bias: alpha 1991.000 (was 1991.000), beta 2014.000 (was 2017.000) counts, 100-sample average
100%
Current-sense bias: PASS alpha 1991.0, beta 2014.0 ct (saved)  (2.8 s)
node 13:
 1. Current-sense bias                 PASS         3 s  alpha 1991.0, beta 2014.0 ct (saved)
    Total                                       0.0 min
node 13: the steps that passed are saved to NV
$ puckutility --can can3 --id 13 -y --json calibrate offsets
...(the same progress text)...
{
  "13": [
    {
      "detail": "alpha 1990.0, beta 2014.0 ct (saved)",
      "seconds": 2.800549505991512,
      "status": "pass",
      "step": "bias",
      "title": "Current-sense bias"
    }
  ]
}
```
The JSON is the last thing printed: one list of step results per node.

### 2.10 Reset: `reset`
```
$ puckutility --can can3 --id 13 -y reset
Resetting node 13...
```

---

## 3. Calibrating an actuator from the command line

### 3.1 The four steps

The task names four calibrations. Steps 3 and 4 come from **one** sweep,
so the CLI runs three steps:

| Task item | CLI name | Step | Moves the motor | Time | Stores |
|---|---|---|---|---|---|
| 1. current offsets | `offsets` | Current-sense bias | no (bridge off, 100 samples) | 3 s | 0x3008,3 / 0x3009,3 (5.x also 0x3009,9) |
| 2. current gainfactor | `gainfactor` | Current-sense gain | yes: a rotating field at the cal current (0x3011,11), 48 angles | 10 s | 0x3009,6 (beta's gain relative to alpha) |
| 3. encoder offset | `encoder-offset`, `ezero` | Electrical zero + winding order | yes: one revolution each way | 27 s | 0x3011,1 e-zero, 0x3011,2 polarity |
| 4. electrical zero + winding order | (same) | (same sweep) | | | |

`calibrate ab` runs all three in that order. Asking for both
`encoder-offset` and `ezero` runs the sweep once.

**Nothing else is calibrated, on either firmware.** 5.x has further steps
(encoder runout, cogging, slope, fold, friction); the A/B does not run them,
so on 5.x the runout correction and cogging feed-forward are off. Read the
5.x results as "5.x after the same calibration as 4.3.5", not "5.x fully
calibrated".

### 3.2 Order and what it depends on

1. **Load the CSV first.** It carries the cal current, the pole pairs, the
   PWM rate and the gains the steps use. After a wipe the gain factor is 0
   and the drive cannot be enabled at all.
2. **Offsets before gain.** The gain fit measures current; with a wrong
   offset it fits the offset.
3. **Gain before the encoder sweep.** The sweep holds the field with the
   current loop.

The CSV load reboots by itself; the steps need no reboot after them. The
things a reboot latches (PWM rate 0x3001,1, pole pairs 0x3011,3) come from
the CSV, not from calibration.

### 3.3 Reading the results

Bench values (42 V) for comparison. They should repeat within a few counts.

| | Bias alpha/beta (counts) | Gain factor | e-zero, polarity |
|---|---|---|---|
| node 4 BO4523, 4.3.5 | 1993/2062 | 4123 | 50, +1 |
| node 4 BO4523, 5.0.5 | 2005/2068 | 4087 | 50, +1 |
| node 13 BO3518, 4.3.5 | 1991/2014 | 4195 | 533, +1 |
| node 13 BO3518, 5.0.5 | 1995/2029 | 4159 | 533, +1 |

- Bias near 2048 (half the 12-bit ADC). `ab_testing.py run` refuses
  anything outside 1500..2600.
- Gain factor near 4096 (= 1.0). `run` refuses outside 2048..8192.
- e-zero must repeat to a few counts between runs and between firmwares.
  The sweep's "scatter" (about +-15..20 deg electrical on these geared
  actuators) is friction and cogging in the gearhead, not an error.

### 3.4 Failures

- **A step reports FAIL.** The detail column says why. The steps before it
  are saved; fix the cause and run the failed step on its own.
- **"Gainfactor is 0: the drive cannot be enabled..."** after a wipe:
  expected. Load the CSV, then `calibrate ab`.
- **The gain step trips the supply.** The cal current is 3 A; check the
  supply limit (section 1.3).
- **The encoder sweep fails or the polarity flips between runs.** The output
  is not free, or the motor phases or encoder are not the ones the CSV was
  made for.
- **`run` says "the current sense is not calibrated for ..."**. Run
  `calibrate offsets gainfactor`, or `prepare` again.

---

## 4. The A/B procedure

`abtest/ab_testing.py` drives the whole test. It needs the CSVs in
`abtest/csv/` and the firmware images in `abtest/firmware/` (or `--csv`,
`--bin`).
```
python abtest/ab_testing.py --can can3 --id 4 prepare --fw 4.3.5 --actuator BO4523
python abtest/ab_testing.py --can can3 --id 4 run --actuator BO4523 [--profile chirp,square]
python abtest/ab_testing.py plot runs/RUN_A [runs/RUN_B]
python abtest/ab_testing.py compare runs/RUN_A runs/RUN_B
python abtest/ab_testing.py profiles
```
`--runs DIR` sets where results go (default `./runs`).

### 4.1 prepare

`prepare` takes one actuator to a known state on one firmware:
1. `nv save` (restore point, `nv_before.json`);
2. `flash` the firmware (skip with `--no-flash` if it already runs);
3. `version`, which must match `--fw`;
4. `wipe-nvm --no-template --backup prewipe.csv`;
5. `config load` the actuator's CSV for that firmware;
6. `calibrate ab`;
7. read every CSV row back from the drive by SDO; any difference stops it;
8. `nv save` (`nv_after.json`) and `prepare.json` (firmware and CSV SHA-256).

Each stage is the CLI command from section 2 (as `python -m p4core ...`),
and its full output is in `runs/<date>-<time>-n<id>-<fw>-prepare/prepare.log`.
The stages also exist as subcommands (`flash FILE`, `wipe --backup F`,
`load CSV`, `calibrate [STEP...]`) for re-running one.

A real 5.0.5 prepare on node 13 (the console shows each command's last
lines; percent lines dropped):
```
prepare node 13: BO3518 on puck4.app.stm32.release.v5.0.5.bin with P4-32-42V-7A-BO3518-fw5.0.5-v1.2.0-2026-11-06.csv
$ p4core nv save .../nv_before.json
  Reading node 13's NV...
  275 NV value(s) -> .../nv_before.json
$ p4core flash .../puck4.app.stm32.release.v5.0.5.bin
  Rebooting node 13 into its flashloader...
  Node 13: flashloader 3.0.7
  Erasing...
  Programming puck4.app.stm32.release.v5.0.5.bin (89328 bytes)...
  Application validated. Launching...
  Node 13 running 5.0.5
$ p4core version
  5.0.5 <githash>
$ p4core wipe-nvm --no-template --backup .../prewipe.csv
  ...
  Erasing NVM...
  Rebooting...
  WARNING: No template loaded: load the configuration CSV next; the drive cannot be enabled until it is
$ p4core config load .../P4-32-42V-7A-BO3518-fw5.0.5-v1.2.0-2026-11-06.csv
  Loading P4-32-42V-7A-BO3518-fw5.0.5-v1.2.0-2026-11-06.csv into node 13...
  File information: Configure Puck 13
  File version: V1.2.0 (2026-11-06)
  line 11: NMT 0x0101 skipped (Reset node)
  108 value(s) written, 0 error(s)
  Saving to NVM...
  Rebooting...
$ p4core calibrate ab
  Calibration turns the motor (one revolution each way for the encoder zero). Is the motor free to turn? [yes: --yes]
  Calibrating current-sense bias...
  Bias: alpha 1993.938 (was 2048.000), beta 2024.500 (was 2048.000) counts, 100-sample average
  Beta reference correction: reference 1495.38 counts (0x3009,9)
  Calibrating current-sense gain...
  Rotating gain fit: |A| 3482.0 |B| 3440.1 (48 steps, id ~3277 mA)
  Beta gain factor 4146 (was 0, a/b 1.01217, id 3277 mA)
  Calibrating encoder zero (one revolution each way)...
  Encoder zero 533 (was 0, -32.1 deg elec), polarity +1 (was -1); friction split 0.0 deg, points scatter +-15.3 deg (rms 7.7)
0 rows differ, 0 missing
$ p4core nv save .../nv_after.json
  Reading node 13's NV...
  322 NV value(s) -> .../nv_after.json
prepared: 5.0.5 (record in .../runs/20261106-124534-n13-5.0.5-prepare)
```
(The bench ran a 5.0.5 release candidate; its version and file name are
shown here as the release's.) On 5.x the bias prints fractional counts
(Q12.4) and adds the beta reference correction; on 4.3.5 the bias is whole
counts and there is no reference line.

### 4.2 run

`run` drives CSP (mode 8) with SYNC from the host at 50 Hz and 0x60C2 =
20 ms, through these PDOs, mapped in PRE-OPERATIONAL and put back afterwards
(RAM only, nothing saved):

| PDO | Contents | Type |
|---|---|---|
| RPDO1 | 0x6040 controlword, 0x607A target position | SYNC (1) |
| TPDO1 | 0x3008,1 raw alpha, 0x3009,1 raw beta, 0x60EA theta_e, 0x6078 current | SYNC (1) |
| TPDO2 | 0x6064 position, 0x606C velocity | SYNC (1) |
| TPDO3 | 0x60F4 following error, 0x3010,9 iq, 0x3010,12 id (mA) | SYNC (1), 5.x only |

TPDO1/2 are the same data on both firmwares; 4.3.5 transmits only TPDO1 and
TPDO2.

**RPDO1 must be synchronous (type 1).** The drive interpolates from the
target it had at the last SYNC to the one it received for this SYNC. An
asynchronous RPDO (255) lands before the SYNC that should start it, so the
drive takes it as the *start* point and jumps straight to it, and 5.x's
trajectory feed-forward reads a slope of zero. Early runs of this package
made that mistake. They showed 5.0.5 lagging 4.3.5, which was wrong (the
async runs are kept for reference).

The profiles are defined in output-shaft degrees, so they are the same
motion on both firmwares (`profiles` lists them):

| Profile | Motion |
|---|---|
| chirp | position sine swept 0.2 -> 10 Hz (log), constant velocity amplitude until the acceleration limit |
| square | position steps 0, +A, 0, -A at 0.5 and 5 output degrees, 1 s each |
| ramp | constant velocity at 10 and 90 output deg/s, each way, for ripple |
| hold | set-point held still, for the dither at rest |

Each runs twice (`--reps`), and the second half of each repetition mirrors
the first. `run` disables the drive if the following error passes
`--max-error-revs` (4 motor revolutions), if the speed passes the CSV's
maximum, or on any fault, and records every EMCY.

A real 4.3.5 run on node 4:
```
$ python abtest/ab_testing.py --can can3 --id 4 run --actuator BO4523
node 4: firmware 4.3.5
-- chirp: position sine swept 0.2 -> 10 Hz (log), constant velocity amplitude until the acceleration limit
   20261106-131356-n4-4.3.5-chirp: pos err rms 0.680 deg, peak 1.135 (lag 32 ms, then rms 0.047); vel err rms 11.00 deg/s; iq rms 308 mA, peak 1222; iq vs 0x6078 r +0.546; TPDO {'tpdo1': 4102, 'tpdo2': 4102}
-- square: position steps 0, +A, 0, -A at 0.5 and 5 output degrees, 1 s each
   20261106-131418-n4-4.3.5-square: pos err rms 0.250 deg, peak 2.896 (lag 31 ms, then rms 0.031); vel err rms 8.71 deg/s; iq rms 290 mA, peak 970; iq vs 0x6078 r +0.914; TPDO {'tpdo1': 1000, 'tpdo2': 1000}
-- ramp: constant velocity at 10 and 90 output deg/s, each way, for ripple
   20261106-131449-n4-4.3.5-ramp: pos err rms 0.666 deg, peak 1.152 (lag 32 ms, then rms 0.040); vel err rms 2.62 deg/s; iq rms 251 mA, peak 873; iq vs 0x6078 r +0.716; TPDO {'tpdo1': 1518, 'tpdo2': 1518}
-- hold: set-point held still, for the dither at rest
   20261106-131501-n4-4.3.5-hold: pos err rms 0.002 deg, peak 0.007; vel err rms 0.06 deg/s; iq rms 68 mA, peak 174; iq vs 0x6078 r -0.112; TPDO {'tpdo1': 600, 'tpdo2': 600}
```
Each profile writes `runs/<date>-<time>-n<id>-<fw>-<profile>/` with:
- `meta.json`: firmware, githash, node, actuator, calibration values, bus
  voltage and temperature before and after, SYNCs sent, late SYNCs, TPDO
  frame counts, EMCYs, the profile definition;
- `data.npz`: the time base, set-points and every TPDO field;
- `metrics.json` and `plot.png`.

### 4.3 A valid run

Accept a run only if, in its `meta.json`:
- `emcy` is empty;
- `late_syncs` is 0 (a SYNC more than 1.5 periods late; a busy host PC
  causes it);
- every `tpdo_frames` count equals `syncs`, or is within a frame or two;
- `bus_v_before`/`after` are the supply (0.1 V units: 410 = 41.0 V);
- the console printed no `WARNING`.

### 4.4 Repetition and order: A B B A

One run per firmware cannot separate the firmware from drift: the gearhead
warms up, its friction changes, the supply sags. Run in the order
**A B B A** on each actuator:
1. `prepare --fw 4.3.5`, then `run`;
2. `prepare --fw 5.0.5`, then `run`;
3. `run` again (5.0.5, no prepare);
4. `prepare --fw 4.3.5`, then `run`.

Compare A1 with A2 and B1 with B2 first: that spread is the run-to-run
noise. A firmware difference counts only where it is clearly larger than
that spread. Say so in the report where it is not.

**Warm-up:** before the first counted run, run `--profile ramp` once and
discard it. Temperatures are recorded before and after every run: compare
like with like, within about 5 C.

---

## 5. The 5.x data logger

5.x can record internal values at up to the control rate (12.5 kHz
on both 5.x CSVs) into a RAM buffer, then send them to the host after the
motion is over. 4.3.5 has no equivalent (section 6). `ab_testing.py` does
not use the logger; this section is for deeper looks.

### 5.1 Objects

| Object | Name | Use |
|---|---|---|
| 0x3004,1 | State | write 1 = arm, 0 = stop, 3 = play back; read 0 idle, 1 collecting, 2 full, 3 playing back |
| 0x3004,2 | Records | records to capture (0 = as many as fit) |
| 0x3004,3 | Decimation | one record every N control cycles (0 or 1 = every cycle) |
| 0x3004,4 / ,5 | Cursor / value | SDO read-out: set the cursor, read one channel per read of ,5 |
| 0x3004,7 | PWM periods per record | latched at arm: record interval = this / PWM rate |
| 0x3004,8 | Records captured so far | |
| 0x3004,15 | Mode | 0 raw (records), 1 data statistics (min, max, mean, stdev per channel), 2 timing (code profiling) |
| 0x3004,18 | Channels latched at arm | |
| 0x3004,9-14 | Stream | a separate feature: an integrated iq (,9 = 5 the current loop's iq, 6 the rotor frame's), cycle count and position snapshotted at each SYNC, for host-side fits. Not needed for the A/B. |

**Channels are TPDO4's mapping (0x1A03).** Up to 16 objects, any numeric type;
a record is the mapped objects laid out as TPDO4's payload. The buffer is
2048 bytes, so records = 2048 / record bytes: two 16-bit channels give 512
records, three 32-bit channels 170. At 12.5 kHz control and decimation 4, a
record is every 320 us, and 170 records cover 54 ms. The record and
decimation limits set the span; plan the capture around the moment you want.

**Playback** sends the records as TPDO4 frames, one per record, at the
event timer: 512 records take about half a second, against seconds by SDO.
It needs the node OPERATIONAL. `p4core.actuator.playback.read()` does the
whole read-out (and returns `None` when playback is not possible, e.g. a
record over 8 bytes; then read by SDO).

### 5.2 Worked example: a burst inside a CSP move

This captures position command and feedback, then velocity, iq and current,
mid-way through a constant-speed CSP move. It is the capture that found the
asynchronous-RPDO problem (section 4.2). It uses the helpers in
`sandbox/testp4.py` and `ab_testing.py`.
```python
import math, struct, sys
import canopen
import testp4 as T                          # puckutility/sandbox
import ab_testing as A                      # puckutility/abtest
from p4core import cia402
from p4core.actuator import playback
from p4core.can_backend import make_network
from p4core.session import P4Network

RATE = 50.0                                 # SYNC, Hz
WORDS = (0x60620020, 0x60640020)            # 0x6062 demand, 0x6064 actual: I32 each
args = T.parse_args(sys.argv[1:])           # --can PORT --id N
net = make_network(args.can, network_cls=P4Network)
try:
    d = T.Drive(net, net.add_node(args.node_id, canopen.ObjectDictionary()), args)
    d.identify()
    ctl = d.r(0x3001, 14, 'I')              # control rate, Hz
    pwm = d.r(0x3001, 1, 'I')               # PWM rate, Hz
    rec_bytes = sum((w & 0xFF) // 8 for w in WORDS)
    records = 2048 // rec_bytes             # what the buffer holds
    dec = max(1, math.ceil(0.08 * ctl / records))   # span about 80 ms
    try:
        A.map_run_pdos(d, ['rpdo1'])
        # TPDO4 = the channels, disabled while it is changed
        cob4 = d.r(0x1803, 1, 'I')
        d.w(0x1803, 1, cob4 | 0x80000000, 'I')
        d.w(0x1A03, 0, 0, 'B')
        for k, w in enumerate(WORDS):
            d.w(0x1A03, k + 1, w, 'I')
        d.w(0x1A03, 0, len(WORDS), 'B')
        d.w(0x3004, 1, 0, 'B')              # stop
        d.w(0x3004, 15, 0, 'B')             # raw mode
        d.w(0x3004, 2, records, 'H')
        d.w(0x3004, 3, dec, 'H')
        T.enter(d, cia402.MODE_CYCLIC_SYNC_POS, RATE)
        A._rpdo1(d, True)                   # RPDO1 valid, SYNC type
        p = float(d.r(0x6064, 0, 'i'))
        v = T.cts_s(400.0)                  # motor rpm -> counts/s
        t0, vel = d.now(), 0.0
        for k in range(int(3.0 * RATE)):
            vel = min(v, vel + v / (0.3 * RATE)) if k < 125 else max(0.0, vel - v / (0.3 * RATE))
            p += vel / RATE
            net.send_message(0x200 + d.id, struct.pack('<Hi', T.CW_ENABLE, int(round(p))))
            d.send_sync()
            if k == int(2.0 * RATE):
                d.w(0x3004, 1, 1, 'B')      # arm at 2 s, on the plateau
            wait = t0 + (k + 1) / RATE - d.now()
            if wait > 0:
                d.sleep(wait)
        A._rpdo1(d, False)
        dt = d.r(0x3004, 7, 'H') / pwm      # seconds per record
        recs = playback.read(d.node, list(WORDS), records)
        print('%d records, %.0f us apart' % (len(recs), dt * 1e6))
    finally:
        d.stop(); d.restore(); d.unmap_pdos()
        # put TPDO4 back as it was here (omitted)
finally:
    net.disconnect()
```
`recs` is a list of records, each a tuple of the channel values, sign-extended
to the channel's type. On node 4 (12.5 kHz control) this gave 256 records
320 us apart.

---

## 6. Collecting data on 4.x

4.3.5's limits, measured on the bench:

| | 4.3.5 | 5.x |
|---|---|---|
| TPDOs that transmit | TPDO1 and TPDO2 only | all four |
| Event-timer TPDOs (type 254/255 with 0x180x,5) | not implemented: SYNC only | work, down to 1 ms |
| 1 kHz SYNC | works, no frames lost | works |
| Logger | none usable (0x2385 is a torque-step tool that forces IDLE) | 0x3004, section 5 |

So on 4.3.5 everything comes over TPDO1/2 at the SYNC rate. The A/B uses
50 Hz on both firmwares, which is also the set-point rate.

For a faster same-path comparison, run SYNC at 1 kHz and send a set-point
every SYNC, interpolated on the host, with 0x60C2 = 1 ms (its default).
4.3.5 interpolates over the SYNC period it sees, 5.x over 0x60C2 (section 9). That is a different test from the
50 Hz one; label it so.

### 6.1 dq currents from raw alpha/beta

4.3.5 has no usable iq/id object: 0x6078 is filtered and saturates, and
0x3010,6 reads about 0. The A/B therefore rebuilds id/iq on the host from
the raw ADC words and the electrical angle, the same way on both firmwares:
```python
import numpy as np

def ma_per_count(gain, shunt_mohm, vref=3.3):
    """mA per ADC count: (Vref/4096) / (shunt * amplifier gain).  gain is
    0x3008,4 (x1000, e.g. 3530 = 3.53 V/V), shunt 0x3008,5 in milliohm."""
    return vref / 4096.0 / (shunt_mohm * 1e-3 * gain / 1000.0) * 1000.0

def alpha_beta_ma(raw_a, raw_b, cal):
    """cal: bias_a/bias_b in counts (5.x stores Q12.4: divide 0x3008/9,3 by 16),
    gf_a/gf_b = 0x3008/9,6 (4096 = 1.0), gain, shunt."""
    k = ma_per_count(cal['gain'], cal['shunt'])
    ia = (np.asarray(raw_a, float) - cal['bias_a']) * cal['gf_a'] / 4096.0 * k
    ib = (np.asarray(raw_b, float) - cal['bias_b']) * cal['gf_b'] / 4096.0 * k
    return ia, ib

def park(ia, ib, theta_e_i16):
    """0x60EA is I16 full scale = 2 pi.  The firmware's convention."""
    th = np.asarray(theta_e_i16, float) * np.pi / 32768.0
    c, s = np.cos(th), np.sin(th)
    return ia * c + ib * s, -(-ia * s + ib * c)          # id, iq
```
These are the functions in `ab_testing.py` (`ma_per_count`, `alpha_beta_ma`,
`park`); `meta.json` holds each run's `cal` values. Check the sign of iq on
your data: `ab_testing.py` picks the sign under which iq does positive work
while the set-point moves, and reports the correlation with 0x6078
(`iq_6078_corr`) only as a cross-check.

Validation on the bench: on 5.x, this reconstruction against the drive's own
unfiltered iq (0x3010,9) gave r = 0.988 with a fit of 0.98 x firmware - 9 mA.
On 4.3.5 id stayed at sd 70 mA at constant speed. The raw words and the
angle come from the same control cycle: TPDO1 does not tear.

---

## 7. CANopen changes 4.3.5 -> 5.x

Compiled from both firmwares' source and EDS files (`eds/puck4-4.3.5.eds`,
`eds/puck4-5.0.5.eds`), and checked on the bench where marked **[bench]**.
The manuals for both versions are in `docs/`.

### 7.1 Statusword (0x6041)

| | 4.3.5 | 5.x |
|---|---|---|
| Bit 9 (remote) | never set | always set |
| Fault Reaction Active | no such state: a fault goes straight to FAULT | present: the drive ramps down per 0x605E, then FAULT. **[bench]** about 200 ms after a following-error trip, during which a fault reset is lost |
| Bit 7 (warning) | not used for warnings | set by the status-only conditions: i2t, velocity warning, velocity limit, position warning. These send no EMCY |
| Bit 10 (target reached) | PP: end of profile | PP: inside 0x6067 for 0x6068; PV: within 0x606D for 0x606E; under Halt: at rest |
| Bit 11 (internal limit) | 0x6073 clamps the q current | any torque or current limit, or the speed governor; 0x2386 says which |
| Bit 12 | PP set-point acknowledge only | PP: set-point acknowledge. PV: speed zero (0x606F/0x6070). Cyclic modes: following the target, set at the first SYNC, cleared 2 SYNC periods after the last one **[bench]** |
| Bit 13 | homing only | homing; PP, CSP, CSI: following error |

### 7.2 EMCY

The frame layout is the same: code (2 bytes LE), error register 0x1001, then
zeros; the clearing EMCY (code 0000) carries the cleared code in bytes 6-7.
**[bench]** SYNC loss: `80 81 11 00 00 00 00 00`, cleared by
`00 00 00 00 00 00 80 81`, identical on both.

| | 4.3.5 | 5.x |
|---|---|---|
| COB-ID | fixed 0x80 + node; 0x1014 ignored | from 0x1014, bit 31 disables |
| Inhibit 0x1015 | ignored | honoured (in 100 us units, rounded up to 1 ms); up to 8 EMCYs queued |
| 0x1003 history | read-only | writing 0 to 0x1003,0 clears it |

Codes that changed (by 0x1002 status bit):

| Condition | 4.3.5 | 5.x |
|---|---|---|
| Encoder magnet | 0x7321 | 0x7385 |
| i2t | 0x2310, EMCY on set and clear | 0x2310, status only, no EMCY |
| Velocity warning | 0x8411 | 0x8481, status only |
| Velocity tracking fault | 0x8414 | 0x8482 |
| Velocity limit | 0x8418 | 0x8483, status only |
| Position warning | 0x8611 | 0x8680, status only |
| Position following fault | 0x8614 **[bench]** | 0x8611 **[bench]** |
| NMT stop/reset while enabled | (0x8613, never raised) | 0x8190, reaction per 0x6007 |
| New on 5.x | | 0x2390 demagnetisation limit, 0x2391 positive id, 0x2392 hard over-current, 0x3280 fast over-voltage trip, 0x8130 heartbeat consumer timeout, 0x8210 RPDO shorter than its mapping, 0x6320 current-sense calibration lost |
| SYNC timeout (0x2406) | 0x8180 **[bench]** | 0x8180 **[bench]** |

Watch the swap: **0x8611 is a warning on 4.3.5 and the following-error
fault on 5.x.** Decode by firmware version.

5.x fault reactions: encoder, over-current, over-voltage and the hardware
trips stop the bridge at once; tracking and following faults, over-temperature
and SYNC loss ramp down per 0x605E first; bus-off, heartbeat loss and NMT
stop follow 0x6007.

### 7.3 SDO

| | 4.3.5 | 5.x |
|---|---|---|
| Transfers | expedited only (4 bytes max) | expedited and segmented (strings) |
| Unsupported mode written to 0x6060 | 0x05040001 **[bench]** | 0x06090030 **[bench]** |
| Controlword command not allowed in this state (e.g. anything but fault reset while faulted) | 0x05040001 **[bench]** | 0x08000022 **[bench]** |
| 0x6060 written before Operation Enabled | refused (0x05040001): write the mode *after* enabling | stored, applied on entering Operation Enabled |
| PDO mapping written while its sub0 is not 0 | accepted | 0x08000022: set sub0 = 0 first |
| Out-of-range values | mostly not checked | 0x06090030 / 0x06090031 |
| Fault reset (controlword bit 7) | every write with bit 7 set while faulted | **rising edge only** **[bench]**: send bit 7 = 0, then 1 |

Unchanged: unknown object 0x06020000, read-only 0x06010002, PDO mapping
errors 0x06040041/42.

A host that treats a refused mode as "unsupported" must accept both
0x05040001 and 0x06090030.

### 7.4 Identity: 0x100A, 0x3003

| | 4.3.5 | 5.x |
|---|---|---|
| 0x100A firmware version | UNSIGNED32 major<<24 \| minor<<8 \| patch: 4.3.5 reads 0x04000305 | VISIBLE_STRING "5.0.5" (segmented upload) |
| 0x3003 git hash | absent (0x06020000) | UNSIGNED32: bits 0-27 the 7-digit short hash, bit 31 set for a dirty tree |
| 0x1000 device type | 0x0402 | 0x00020192 (CiA 402 servo) |
| 0x1008 device name | "Barr" | "Barrett" |
| 0x1009 hardware version | rw "2" | ro "2.0" |

To tell the lines apart: read 0x100A expedited; 4 bytes that decode to
4.x.x is 4.3.5. `puckutility version` does this.

### 7.5 Stopping (0x605A-0x605E, 0x6007, 0x1029)

**4.3.5:** 0x605A-0x605E are UNSIGNED16 and only 0x605A is used: quick
stop with 0x605A = 5..8 holds position in Quick Stop Active, anything else
drops to Switch On Disabled. Shutdown, Disable operation and Disable voltage
drop the bridge at once. A fault drops the bridge at once. Halt (bit 8) acts
in homing only.

**5.x:** the stop codes are INTEGER16, RAM only, and ramp before the state
changes:

| Object | Valid | Meaning |
|---|---|---|
| 0x605A quick stop | 0-3, 5-7 | 1/5 ramp on 0x6084, 2/6 on 0x6085, 3/7 on the current limit; 5-7 stay in Quick Stop Active |
| 0x605B shutdown, 0x605C disable operation | 0, 1 | 1 = ramp on 0x6084 first |
| 0x605D halt | 1-3 | Halt works in PP, PV, CSP, CSV and the torque modes |
| 0x605E fault reaction | 0-3 | |
| 0x6007 abort connection | 0-3 | 0 none, 1 fault (default), 2 disable voltage, 3 quick stop; for bus-off, heartbeat loss, NMT stop/reset while enabled |
| 0x1029,1 | 0-2 | the NMT state on a communication error: 0 to PRE-OP, 1 no change, 2 STOPPED |

Codes 4 and 8 (stop at the voltage limit) are refused. A stop takes at most
10 s.

### 7.6 Units and scaling

**These matter when moving a value between the two CSVs.**

| Object | 4.3.5 | 5.x |
|---|---|---|
| Current-sense bias 0x3008,3 / 0x3009,3 | whole ADC counts | Q12.4 (counts x 16). 5.x converts a stored 4.3.5 value at boot; 4.3.5 does not convert back (section 1.3) |
| Kt | 0x3011,4 UNSIGNED16, mNm/A rms | 0x3011,14 REAL32, mNm/A rms; 0x3011,4 is a rounded mirror |
| Inductance 0x3011,6 | line-to-line, 0.01 mH | line-to-line, uH, and it means Ld; Lq is 0x3011,12, L_hf 0x3011,13. **x10 between lines** |
| Resistance 0x3011,5 | line-to-line, 0.01 ohm | line-to-line, mohm |
| Torque per-mille (0x6071, 0x6072, 0x6077, 0x60B2, 0x60E0/E1) | of 0x6076 "max torque" | of rated torque 0x6076 = Kt x 0x6075 |
| Current per-mille (0x6073, 0x6078) | of i_peak 0x3011,9 | of rated current 0x6075 = i_cont 0x3011,8 / sqrt 2 |
| 0x6072, 0x6073, 0x60E0, 0x60E1 default | 1000 | 65535 = no limit |
| Encoder direction | 0x3013,3 (+-1) | 0x607E (0x00 / 0xC0) |
| 0x3013,5 | "encoder lag factor" UNSIGNED16 | encoder latency, INTEGER16 us |
| 0x3001,4 dead-time trim | UNSIGNED16 | INTEGER16 ns |
| 0x3025,3 | 0.01 A | mA |
| Velocity feed-forward 0x2382,2 | x/16384 of the **measured** velocity | x/16384 of the **interpolation slope** (0x2382,5 = 1 selects measured) |

So the same per-mille number is a different torque on each line. The two
CSVs of each actuator were converted with these rules and then tuned on
their own firmware; they are not copies of each other.

### 7.7 Floating point

On 4.3.5, 0x2380,1-2 (current kp, ki) and 0x2381,1-2 (velocity kp, ki) are
declared UNSIGNED32 in the EDS but hold IEEE-754 float bits. Read through
the 4.3.5 EDS, convert with:
```python
struct.unpack('<f', struct.pack('<I', value))[0]
```
The 5.x EDS declares them REAL32, along with 0x2381,3-4 (friction FF, settle
ki), 0x2383,1-2 (CSI stiffness, damping), 0x3011,14 (Kt), 0x3011,17-18 (Tc,
B) and 0x3012,4. The 5.x CSVs write these as decimals (`0.11106021`); the
4.3.5 CSVs write the float bits in hex (`0x3e35f607`, the same 0.1777...).

### 7.8 PDOs and SYNC

| | 4.3.5 | 5.x |
|---|---|---|
| SYNC in PRE-OPERATIONAL | ignored | processed (interpolation, logger); PDOs still need OPERATIONAL |
| RPDO type 1-240 | buffered and **replayed at every SYNC**, even with no new frame, even with the COB-ID invalid | applied at the next SYNC only if a new frame arrived |
| RPDO type change | takes effect only when the mapping count 0x160x,0 is rewritten: write the type, then the count | immediate |
| RPDO types 241-253 | | refused |
| TPDOs that transmit | **TPDO1 and TPDO2 only [bench]** | all four |
| TPDO type 0 | every SYNC | at a SYNC, only if the payload changed |
| TPDO type 254/255 | never sent | sent on the event timer 0x180x,5 only (no change-of-state) |
| 0x1016 heartbeat consumer | absent | one consumer; timeout -> EMCY 0x8130 |
| 0x1011 restore defaults | absent | ,3 and ,5 ("load"), effective at reset |
| 0x1010,5 | absent | save the manufacturer area |

The 4.3.5 replay is why `testp4.py` and `ab_testing.py` make RPDO1 type
255 whenever they invalidate it.

### 7.9 Modes (0x6502, 0x6060)

0x6060/0x6061 changed from UNSIGNED8 to INTEGER8.

| | 0x6502 | Modes |
|---|---|---|
| 4.3.5 | 0x00000BAD | 1 PP, 3 PV, 4 PT, 6 HM, 8 CSP, 9 CSV, 10 CST, 12 PVA |
| 5.x | 0x000307AD | 1 PP, 3 PV, 4 PT, 6 HM, 8 CSP, 9 CSV, 10 CST, 11 CSTCA, -1 PVA, -2 CSI |

- **11 CSTCA** (new): cyclic torque, commutated at the angle the host
  writes to 0x60EA.
- **-2 CSI** (new): cyclic impedance; position, velocity and torque
  targets with stiffness and damping 0x2383,1-2 (both 5.x CSVs set them).
- **PVA** moved from 12 to -1 (open-loop phase voltage and angle).

### 7.10 Objects added and removed

Removed in 5.x: 0x2110-0x2112 (user peak/continuous current, peak time),
0x3013,3 (encoder direction, now 0x607E).

Added in 5.x:
- Communication: 0x1010,5; 0x1011; 0x1016; 0x1029.
- CiA 402: 0x6007; 0x6067/0x6068 position window and time; 0x606F/0x6070
  velocity threshold and time; 0x6075 rated current; 0x6077 torque actual;
  0x6079 DC link voltage; 0x6088; 0x6099 homing speeds; 0x60E3 homing methods.
- Control: 0x2381,3-4 friction FF and settle ki; 0x2382,5 velocity FF source;
  0x2383 CSI gains; 0x2384,10 fast over-voltage trip; 0x2386 binding limits.
- Board and current sense: 0x3000,5; 0x3001,11-15 (Vbus trim, dead-time
  ramp, K = PWM periods per control cycle, control rate, max K); 0x3008,7-8,
  0x3009,7-9 (slope, offsets, reference cal); 0x3010,7-13 (calibrated
  alpha/beta mA, iq, id and other telemetry); 0x3019,5-6.
- Motor: 0x3011,12-18 (Lq, L_hf, Kt REAL32, working peak current, binding
  limits, Tc, B); 0x3012,4 sub-count position.
- Encoder: 0x3013,7-15 (latency, sub-count, chip, filter, observer corner
  and feed-forward).
- Tools: 0x3003 git hash; 0x3004 logger (section 5); 0x3015-0x3017
  injection and debug; 0x3027 encoder runout table; 0x302A code timing;
  0x302B purr generator; 0x302C CAN frame-loss counters; 0x302D clock
  calibration; 0x302E cogging table.

Also changed type or default: 0x3012,2 INTEGER32 -> INTEGER16; 0x6074 now
read-only; 0x6098 default 37; 0x220A default 80; 0x1200,1-2 read-only.

---

## 8. Velocity estimation

0x606C is computed differently on the two firmwares, so "set-point
velocity - 0x606C" partly compares estimators, not motion. That is why
`compare` reports the velocity error against a host-side d(0x6064)/dt
(`vel_err_rms_dps`, the same for both) and keeps 0x606C as a second column
(`vel_err_606c_rms_dps`).

**4.3.5.** Every 1 ms the change in 0x6064 over that millisecond (whole
counts, so steps of 1000 counts/s), through a 2nd-order Butterworth
low-pass at 0x2100,1 Hz (both 4.3.5 CSVs: 200 Hz). The filter is built at
boot: a change needs Save and a reboot. The filtered value also feeds the
current loop's decoupling, so its lag reaches the current loop.

**5.x.** A tracking observer (a PLL on the encoder angle) at the control
rate (12.5 kHz on both 5.x CSVs: 100 kHz PWM, K = 2),
with sub-count resolution and the runout correction (off in the A/B: not
calibrated). Its output is averaged over each 1 ms, then optionally filtered
by a one-pole at 0x2100,1 (both 5.x CSVs: 0 = off).
- 0x3013,14 is the observer corner (Hz, minimum 20). It is critically
  damped, so it has no steady error on a constant acceleration. A lower corner
  gives a quieter 0x606C and a slower response to torque changes.
- 0x3013,15 feeds the commanded torque into the observer (percent, needs Kt
  and J set). With it, a low corner only filters sensor noise.
- Shipped: BO4523 70 Hz / 100 %, BO3518 150 Hz / 100 %, chosen in
  commissioning.
- Both take effect within 1 ms; 0x2100,1 takes effect in IDLE.

The same 0x2100,1 value therefore means a different filter on each line
(a biquad on 4.3.5, a one-pole on 5.x).

| | Lag (estimate) | Noise |
|---|---|---|
| 4.3.5 | about 0.5 ms (the 1 ms difference) + 1.1 ms (200 Hz Butterworth) | 1000 counts/s quantisation, smoothed by the filter |
| 5.x | about 0.5 ms (the 1 ms mean) + a few 1/(2 pi f0) on a torque change, none on a steady ramp | sub-count angle at the control rate; set by the corner |

The lag figures are arithmetic, not measurements. The bench result that
matters is in section 10.3: 5.0.5's velocity error is lower in every moving
profile on both estimates.

**Aliasing.** The 50 Hz TPDOs sample, they do not average. Velocity
derived from them is valid only for motion slower than about 10 Hz. Inside a
SYNC period, use the 5.x logger (section 5).

---

## 9. SYNC rate and 0x60C2

CSP interpolates linearly from the target it had at one SYNC to the target
that arrived for the next, at 1 kHz on both firmwares. Which period it
spreads that over differs:
- **4.3.5** re-measures every SYNC: the interpolation period *is* the SYNC
  period. 0x60C2 is not used for it.
- **5.x** uses 0x60C2 only (0x60C2,1 x 10^0x60C2,2 s; here 20 x 10^-3),
  as CiA 402 has it. It does not measure SYNC. 0x60C2 is read when the
  mode is entered and again at every SYNC, so a write in the mode applies
  from the next SYNC.

So set 0x60C2 to the SYNC period you run. On 5.x a mismatch shows: a 0x60C2
longer than SYNC stops short of every target, and one shorter than half the
SYNC period clears statusword bit 12 between SYNCs (and drops CSV/CST
targets to zero until the next one). The A/B runs 50 Hz SYNC with
0x60C2 = 20 ms on both. The target must reach the drive in an RPDO of type 1
(section 4.2).

**When SYNC stops**, the drive holds the last target and, on 5.x, statusword
bit 12 drops (target no longer followed) after two 0x60C2 periods. Whether it also
faults is set by 0x2406, the SYNC timeout in ms:
- 0x2406 = 0 (all four CSVs): no fault. 4.3.5 stays enabled, holding; 5.x
  holds and clears bit 12.
- 0x2406 = 100: both firmwares send EMCY 0x8180 (SYNC timeout) about 90 ms
  after the last SYNC and fault. **This happens in IDLE too**, so a non-zero
  0x2406 faults the drive whenever a tool talks to it without SYNC running.
  Set 0x2406 only while SYNC flows, and put it back before SYNC stops.

The A/B keeps 0x2406 = 0: `ab_testing.py` owns the SYNC and checks for late
SYNCs itself (section 4.3). A fault reset with SYNC still missing trips again
at once on both.

---

## 10. Analysis recipe

### 10.1 Plots
```
$ python abtest/ab_testing.py plot runs/20261106-131449-n4-4.3.5-ramp runs/20261106-130811-n4-5.0.5-ramp
```
draws, against time, Id and Iq, the velocity error and the position error,
one firmware over the other. One run alone gives its own figure (`plot.png`
is written by `run` too).

### 10.2 Metrics
```
$ python abtest/ab_testing.py compare runs/20261106-131449-n4-4.3.5-ramp runs/20261106-130811-n4-5.0.5-ramp
                            4.3.5 4.3.5    5.0.5 5.0.5
pos_err_rms_deg                  0.6663         0.2458
pos_err_peak_deg                  1.152          0.496
vel_err_rms_dps                   2.618          1.377
vel_err_606c_rms_dps              4.608          3.019
iq_rms_ma                         251.3          227.9
iq_peak_ma                        872.7          620.4
id_rms_ma                          54.8          47.72
id_peak_ma                        206.3          279.8
iq_6078_corr                     0.7163          0.965
still_pos_sd_deg               0.008977         0.0164
still_iq_rms_ma                   90.93            131
moving_iq_sd_ma                   276.3          245.5
lag_ms                               32             24
pos_err_at_lag_rms_deg          0.04031        0.03702
```
It also writes `compare.json`. All angles are output-shaft degrees.

| Metric | Meaning |
|---|---|
| pos_err_rms/peak_deg | host set-point - 0x6064 |
| vel_err_rms_dps | set-point derivative - host d(0x6064)/dt: the same estimator for both firmwares |
| vel_err_606c_rms_dps | set-point derivative - 0x606C: includes each firmware's own velocity estimator (section 8) |
| iq/id rms/peak | the reconstruction of section 6.1 |
| iq_6078_corr | correlation of the reconstructed iq with 0x6078 (sanity check; low on 4.3.5, whose 0x6078 is filtered) |
| still_* | the held segments before and after the motion |
| moving_iq_sd_ma | iq ripple while moving |
| lag_ms | the delay that best fits 0x6064 to the set-point |
| pos_err_at_lag_rms_deg | the position error after removing that delay: tracking shape, not delay |

The lag and the error after the lag answer different questions. The lag
is what a user sees at the output. The error after the lag shows whether
the firmware follows the shape of the trajectory.

### 10.3 Bench results (42 V, single runs)

| | Lag 4.3.5 / 5.0.5 | Pos err RMS ramp | chirp | square peak | After the lag, RMS |
|---|---|---|---|---|---|
| node 4 BO4523 | 32 / 24 ms | 0.67 / 0.25 deg | 0.68 / 0.27 | 2.9 / 1.0 deg | 0.03-0.05 both |
| node 13 BO3518 | 30 / 21 ms | 0.59 / 0.10 deg | 0.55 / 0.09 | 2.8 / 0.8 deg | 0.04 / 0.06-0.08 |

4.3.5 trails by about 1 output degree at 90 deg/s: its velocity
feed-forward (0x2382,2) acts on the measured velocity, so it has no
trajectory feed-forward. 5.0.5 feeds forward the interpolation slope and
trails by about 0.1 deg. The hold is the same on both (0.001-0.004 deg RMS).
These are single runs. The A B B A repeat (section 4.4) is what makes them a
result.

### 10.4 The report

Per actuator: the compare table for each profile with the A1/A2 and B1/B2
spread beside it, the overlay plots for chirp and square, the calibration
values of each prepare (`prepare.log`), the firmware and CSV SHA-256
(`prepare.json`), and any run discarded and why.

---

## 11. Troubleshooting

- **`scan` finds nothing.** Check the bitrate (1 Mbit/s), the termination
  (120 ohm at each end) and the supply. `ip -d link show` should report
  ERROR-ACTIVE; BUS-OFF means a wiring or bitrate fault. With
  `restart-ms 100` the interface recovers by itself once the fault is gone;
  without it, take the link down and up.
- **A node answers at 127 instead of its ID.** Its NV was erased (for
  example by a JTAG flashloader update), or it is sitting in its
  flashloader. `info --id 127` tells which. Set the ID back with `set-id`,
  then restore its NV.
- **`flash` fails at once.** Wrong image type for the flashloader: 3.0.x
  needs `.bin`, 3.1.0 needs `.ebin` (section 2.8).
- **4.3.5 behaves wildly after a downgrade.** The bias trap (section 1.3):
  `nv save` the node and check 0x3008,3 / 0x3009,3. Values near 32000 mean
  5.x NV. Run `prepare` (wipe, load, calibrate) before enabling again.
- **EMCY 0x3220 or a reset during a step.** The supply browned out:
  under-voltage. Raise the supply's current limit or slow the profile.
- **EMCY 0x8180 (SYNC timeout).** 0x2406 is non-zero and SYNC stopped
  (section 9). The CSVs ship 0 and `run` puts the drive back as it found it.
- **EMCY 0x8611 / 0x8614 (following error).** On 5.x 0x8611 is the fault. On
  4.3.5 0x8611 is a warning and 0x8614 the fault. The output is blocked, or
  the CSV does not match the actuator. `run` also stops itself at 4 motor
  revolutions of error.
- **`run` refuses: "0x6091 = a/b, but ACTUATOR is c/d".** The wrong CSV is
  loaded for this actuator, or `--actuator` is wrong.
- **`run` warns that iq runs against 0x6078.** Expected on 4.3.5 hold and
  sometimes on square (back-and-forth steps make the sign ambiguous). On a
  5.x ramp or chirp, check the bias and gain calibration.
- **`late_syncs` above 0.** The host PC was busy. Close other programs and
  repeat the run.
