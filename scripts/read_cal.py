#!/usr/bin/env python3
"""READ-ONLY calibration / config audit for a Puck4 PMSM controller.

Reads EVERY calibration + config value off the puck over CANopen SDO, prints a
grouped report, saves a timestamped copy to scripts/callogs/, compares each value
to the model's "blank" baseline template, and FLAGS anomalies -- so you can
inspect a damaged puck, validate a fresh cal, and spot cal values that could
cause problems (e.g. a bad cal that DOUBLED L, or a corrupt current-sense slope).

NEVER writes to the puck. Works on OLD firmware (v4.3.2 and earlier) AND new fw:
every SDO read is wrapped -- an abort/timeout means the entry is absent on that
firmware and is reported "MISSING", the report still completes.

Per-value STATUS:
  OK       -- matches baseline or within a sane range.
  DEFAULT  -- still at the blank/uncalibrated value (slope/offset 0, gain sentinel,
              gain at nominal 7490, etc.) -- informational, not an error.
  SUSPECT  -- outside a sane range; could cause problems. The WHY is printed.
  MISSING  -- absent on this firmware (older fw lacks slope :7 / offset :8, etc.).

Usage:   scripts/read_cal.py [can_device] [node_id] [--model P4-16] [--ref some.csv]
         defaults: can0 127
         --model  force the baseline model (else auto-detected from 0x1018:2)
         --ref    use a real known-good config CSV (instead of the blank) as the
                  baseline -- needed for the R/L ratio check to be meaningful,
                  since the blank carries only placeholder R/L.

GUI reuse: the core is read_cal(node, ...) -> dict; it takes an already-connected
canopen node, does the reads + comparisons, writes the log, and returns the
report. main() is a thin CLI wrapper (connect / call / print).
"""
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import can_backend

# Where the per-model "blank" templates (and real configs) live.
BASELINE_DIR = "/home/bailey/pucktuner"
EDS = os.path.join(ROOT, "puck4.eds")

# ----------------------------------------------------------------------------- #
# Baseline (blank template) loader
# ----------------------------------------------------------------------------- #
def load_baseline(path):
    """Parse a pucktuner CSV (`Name,TAG,0xIDX,SUB,TYPE,VALUE`) into
    {(idx, sub): int_value}. Skips header/comment rows, rows without a numeric
    index, and values that aren't plain ints (expressions with $ID, etc.)."""
    base = {}
    if not path or not os.path.isfile(path):
        return base
    with open(path) as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(",")
            if len(parts) < 6:
                continue
            idx_s, sub_s, val_s = parts[2].strip(), parts[3].strip(), parts[5]
            if not idx_s.lower().startswith("0x"):
                continue
            # strip trailing inline "# ..." comment from the value cell
            val_s = val_s.split("#", 1)[0].strip()
            try:
                idx = int(idx_s, 0)
                sub = int(sub_s, 0)
                val = int(val_s, 0)
            except ValueError:
                continue          # $ID expressions, blanks, etc. -> no baseline
            base[(idx, sub)] = val
    return base


def baseline_path_for_model(model):
    if not model:
        return None
    p = os.path.join(BASELINE_DIR, "{}-Blank.csv".format(model))
    return p if os.path.isfile(p) else None


# ----------------------------------------------------------------------------- #
# Low-level SDO read (read-only, firmware-tolerant)
# ----------------------------------------------------------------------------- #
class _Absent(Exception):
    """Entry not present / not readable on this firmware."""


def _read_bytes(node, idx, sub):
    last = None
    for _ in range(3):                       # ride through transient CAN busy-ness
        try:
            return node.sdo.upload(idx, sub)
        except Exception as e:               # SDO abort, timeout, etc.
            last = e
    raise _Absent(str(last))


def _read_int(node, idx, sub, signed):
    return int.from_bytes(_read_bytes(node, idx, sub), "little", signed=signed)


def _read_float(node, idx, sub):
    """U32-packed IEEE754 float. 0xFFFFFFFF is the firmware 'auto' sentinel."""
    raw = _read_bytes(node, idx, sub)
    u = int.from_bytes(raw, "little", signed=False)
    if u == 0xFFFFFFFF:
        return None, u                       # None => auto/uncalibrated sentinel
    return struct.unpack("<f", raw[:4].ljust(4, b"\x00"))[0], u


# ----------------------------------------------------------------------------- #
# Status helpers  (each check returns (status, why))
# ----------------------------------------------------------------------------- #
OK, DEFAULT, SUSPECT, MISSING = "OK", "DEFAULT", "SUSPECT", "MISSING"


def _band(v, lo, hi, why_lo, why_hi):
    if v < lo:
        return SUSPECT, why_lo
    if v > hi:
        return SUSPECT, why_hi
    return OK, ""


# ----------------------------------------------------------------------------- #
# The entry table.  Each entry:
#   (idx, sub, name, signed, kind, unit, checker)
# kind: 'int' | 'float' | 'hex'
# checker(value, base) -> (status, why) | None  (None => generic default check)
#   value = decoded number (or None for the float auto-sentinel)
#   base  = baseline raw int for (idx,sub), or None if not in the template
# ----------------------------------------------------------------------------- #
def _chk_gain(nominal_hint):
    def f(v, base):
        ref = base if base else nominal_hint
        if v == 0:
            return DEFAULT, "gain 0 -- uncalibrated"
        if ref and (v > 1.6 * ref or v < 0.5 * ref):
            return SUSPECT, "gain {} far from nominal {}".format(v, ref)
        if base is not None and v == base:
            return DEFAULT, "at blank nominal {}".format(base)
        return OK, ""
    return f


def _chk_shunt(v, base):
    if v == 0:
        return SUSPECT, "shunt 0 mOhm -- current scaling broken"
    if base is not None and v == base:
        return OK, "matches blank {} mOhm".format(base)
    if not (5 <= v <= 100):
        return SUSPECT, "shunt {} mOhm outside sane 5-100".format(v)
    return OK, ""


def _chk_gainfactor(v, base):
    # Q4.12 calibration multiplier; 4096 == 1.0
    if v == 0:
        return DEFAULT, "gainfactor 0 -- uncalibrated"
    if not (2048 <= v <= 8192):              # 0.5 .. 2.0
        return SUSPECT, "gainfactor {} (={:.3f}) outside 0.5-2.0".format(v, v / 4096.0)
    return OK, ""


def _chk_slope(v, base):
    # Q4.12 signed current-sense slope kA/kB. Doc: sane |k| ~0.15-0.22 mA/mA ->
    # ~600-900 in Q4.12. Flag |k| > 900 (steeper than the 0.22 mA/mA ceiling) as
    # SUSPECT (we have seen a corrupt -793). 0 => uncalibrated/DEFAULT.
    if v == 0:
        return DEFAULT, "slope 0 -- new-fw slope cal not run"
    if abs(v) > 900:
        return SUSPECT, "|slope| {} (={:.3f} mA/mA) > sane 0.22 ceiling".format(v, v / 4096.0)
    return OK, "{:.3f} mA/mA".format(v / 4096.0)


def _chk_offset(v, base):
    # Drive-gated iSense offset a0/b0, signed mA.
    if v == 0:
        return DEFAULT, "offset 0 -- drive-gated offset cal not run"
    if abs(v) > 100:
        return SUSPECT, "|offset| {} mA > 100 mA -- large idle-current correction".format(v)
    return OK, ""


# Bias storage FORMAT changed across firmware: newer fw (>= 4.4.0) stores the 0-A ADC
# reading as Q12.4 (ADC count x 16); older fw (<= 4.3.3) stores it as RAW ADC counts.
# The switch landed at fw commit 5211b03 ("calibration offset units fix"), which is
# after v4.3.3. read_cal() sets this from the detected fw version before the checks run.
#   True  -> Q12.4 (divide by 16)     False -> raw ADC (as-is)     None -> unknown fw
_BIAS_Q124 = None

def _chk_bias(v, base):
    if v == 0:
        return DEFAULT, "bias 0 -- uncalibrated"
    # Convert the stored bias to ADC counts per the firmware-version-gated format.
    if _BIAS_Q124 is True:
        counts, fmt = v / 16.0, "Q12.4"
    elif _BIAS_Q124 is False:
        counts, fmt = float(v), "raw"
    else:                                  # unknown fw: 12-bit ADC maxes at 4095 raw
        counts, fmt = ((v / 16.0), "auto-Q12.4") if v > 4095 else (float(v), "auto-raw")
    # Current sense is bidirectional (mid-rail biased) -> 0-A sits ~2048 counts.
    if counts < 20 or counts > 4076:
        return SUSPECT, "bias {} (={:.0f} ADC cts, {}) RAIL-PINNED -- dead/failed sense channel?".format(v, counts, fmt)
    if not (1500 <= counts <= 2600):
        return SUSPECT, "bias {} (={:.0f} ADC cts, {}) off mid-scale ~2048".format(v, counts, fmt)
    return OK, "{:.0f} ADC counts (0-A ~mid-scale, {})".format(counts, fmt)


def _chk_R(v, base):
    # 0x3011:5, 0.01 Ohm units. Blank carries only a placeholder (100); a real
    # nominal needs --ref. 100/10 placeholders read as DEFAULT.
    if v in (0, 100):
        return DEFAULT, "R {} (=0.01 Ohm units) at placeholder/blank".format(v)
    if base is not None and base not in (0, 100) and (v > 1.5 * base or v < 0.6 * base):
        return SUSPECT, "R {} far from ref {} (>1.5x or <0.6x)".format(v, base)
    return OK, "{:.2f} Ohm".format(v / 100.0)


def _chk_L(v, base):
    # 0x3011:6, 0.01 mH units. See _chk_R re: placeholder. The bad-cal signature
    # we chase (L doubled 150->318) only trips when --ref gives a real nominal.
    if v in (0, 10):
        return DEFAULT, "L {} (=0.01 mH units) at placeholder/blank".format(v)
    if base is not None and base not in (0, 10) and (v > 1.5 * base or v < 0.6 * base):
        return SUSPECT, "L {} far from ref {} (>1.5x or <0.6x -- possible doubled-L bad cal)".format(v, base)
    return OK, "{:.2f} mH".format(v / 100.0)


def _chk_zero_is_default(v, base):
    return (DEFAULT, "0 -- uncalibrated") if v == 0 else (OK, "")


def _chk_pwm(v, base):
    if not (20000 <= v <= 160000):
        return SUSPECT, "PWM {} Hz outside legal 20k-160k".format(v)
    return OK, "{:.0f} kHz".format(v / 1000.0)


def _chk_deadtime(v, base):
    if v == 0 or v > 2000:
        return SUSPECT, "dead_time {} ns outside sane 1-2000".format(v)
    return OK, ""


def _chk_settling(v, base):
    if v > 5000:
        return SUSPECT, "settling {} ns implausibly long".format(v)
    return OK, ""


def _chk_encres(v, base):
    if v == 0:
        return SUSPECT, "encoder resolution 0 -- feedback broken"
    if v & (v - 1):
        return OK, "{} cts (not power-of-two)".format(v)
    return OK, "{} cts".format(v)


def _chk_float_gain(v, base):
    if v is None:
        return DEFAULT, "0xFFFFFFFF sentinel -- auto (firmware computes from R/L)"
    if v <= 0 or v != v:                       # <=0 or NaN
        return SUSPECT, "gain {} not positive/finite".format(v)
    return OK, ""


ENTRIES = [
    ("PRODUCT / VERSION", [
        (0x1018, 1, "Vendor ID",           False, "hex", "", None),
        (0x1018, 2, "Product code",        False, "hex", "", None),
        (0x1018, 3, "Revision",            False, "hex", "", None),
        (0x1018, 4, "Serial number",       False, "int", "", None),
        (0x100A, 0, "Software version",    False, "int", "(packed)", None),
        (0x1009, 0, "Hardware version",    False, "hex", "", None),
    ]),
    ("MOTOR (0x3011)", [
        (0x3011, 1, "e_zero (enczero)",    False, "int", "cts",         _chk_zero_is_default),
        (0x3011, 2, "e_polarity",          True,  "int", "",            None),
        (0x3011, 3, "Poles",               False, "int", "",            _chk_zero_is_default),
        (0x3011, 4, "Kt",                  False, "int", "mNm/A",       _chk_zero_is_default),
        (0x3011, 5, "R",                   False, "int", "0.01ohm",     _chk_R),
        (0x3011, 6, "L",                   False, "int", "0.01mH",      _chk_L),
        (0x3011, 7, "J",                   False, "int", "gcm^2*2^15",  None),
        (0x3011, 8, "i_cont",              False, "int", "mA",          None),
        (0x3011, 9, "i_peak",              False, "int", "mA",          None),
        (0x3011, 10, "i_peak_time",        False, "int", "ms",          None),
        (0x3011, 11, "i_cal",              False, "int", "mA",          _chk_zero_is_default),
    ]),
    ("CURRENT SENSE -- ALPHA (0x3008)", [
        (0x3008, 3, "Bias (Q12.4)",        False, "int", "",       _chk_bias),
        (0x3008, 4, "Gain (*1000)",        False, "int", "",       _chk_gain(7490)),
        (0x3008, 5, "Shunt",               False, "int", "mOhm",   _chk_shunt),
        (0x3008, 6, "Gainfactor (Q4.12)",  False, "int", "",       _chk_gainfactor),
        (0x3008, 7, "Slope kA (Q4.12)",    True,  "int", "",       _chk_slope),   # new fw
        (0x3008, 8, "Offset a0",           True,  "int", "mA",     _chk_offset),  # new fw
    ]),
    ("CURRENT SENSE -- BETA (0x3009)", [
        (0x3009, 3, "Bias (Q12.4)",        False, "int", "",       _chk_bias),
        (0x3009, 4, "Gain (*1000)",        False, "int", "",       _chk_gain(7490)),
        (0x3009, 5, "Shunt",               False, "int", "mOhm",   _chk_shunt),
        (0x3009, 6, "Gainfactor (Q4.12)",  False, "int", "",       _chk_gainfactor),
        (0x3009, 7, "Slope kB (Q4.12)",    True,  "int", "",       _chk_slope),   # new fw
        (0x3009, 8, "Offset b0",           True,  "int", "mA",     _chk_offset),  # new fw
    ]),
    ("ENCODER (0x3013)", [
        (0x3013, 1, "Resolution",          False, "int", "cts",    _chk_encres),
        (0x3013, 2, "User zero",           True,  "int", "cts",    None),
        (0x3013, 3, "User polarity",       True,  "int", "",       None),
        (0x3013, 4, "Type",                False, "int", "",       None),
        (0x3013, 5, "Lag factor",          False, "int", "",       None),
        (0x3013, 6, "H/W filter",          False, "hex", "",       None),
    ]),
    ("GAINS", [
        (0x2380, 1, "Current Kp",          False, "float", "V/A",     _chk_float_gain),
        (0x2380, 2, "Current Ki",          False, "float", "V/(A.s)", _chk_float_gain),
        (0x2381, 1, "Velocity Kp",         False, "float", "",        _chk_float_gain),
        (0x2381, 2, "Velocity Ki",         False, "float", "",        _chk_float_gain),
        (0x2382, 1, "Position Kp",         False, "int",   "",        None),
        (0x3024, 2, "Control gain factor", True,  "int",   "0.01",    None),
        (0x3024, 3, "Current zeta",        True,  "int",   "0.01",    None),
    ]),
    ("TIMING (0x3001)", [
        (0x3001, 1, "PWM freq",            False, "int", "Hz",   _chk_pwm),
        (0x3001, 2, "Dead time",           False, "int", "ns",   _chk_deadtime),
        (0x3001, 3, "Max prop delay",      False, "int", "ns",   None),
        (0x3001, 5, "Max settling time",   False, "int", "ns",   _chk_settling),
        (0x3001, 6, "Sampling time",       False, "int", "ns",   None),
        (0x3001, 9, "Nominal bus voltage", False, "int", "V*10", None),
    ]),
    ("THERMAL / i2t", [
        (0x3025, 3, "TempLimited I_cont",  False, "int", "mA",     None),
        (0x3025, 2, "i2t filter cutoff",   True,  "int", "Hz",     None),
        (0x220A, 0, "Motor temp limit",    False, "int", "degC",   None),
        (0x2384, 6, "Bus overvolt limit",  False, "int", "0.1V",   None),
        (0x2384, 7, "Bus undervolt limit", False, "int", "0.1V",   None),
        (0x2384, 9, "Amp temp limit",      False, "int", "degC",   None),
        (0x3026, 1, "Thermistor R divider",False, "int", "Ohm",    None),
        (0x3026, 2, "Thermistor beta",     False, "int", "",       None),
    ]),
]


# ----------------------------------------------------------------------------- #
# Formatting a single decoded value
# ----------------------------------------------------------------------------- #
def _fmt_value(value, uraw, kind):
    if kind == "float":
        if value is None:
            return "AUTO(0xFFFFFFFF)"
        return "{:.5g}".format(value)
    if kind == "hex":
        return "0x{:X}".format(value)
    return str(value)


def _decode_sw_version(u):
    return "{}.{}.{}".format((u >> 24) & 0xFF, (u >> 8) & 0xFFFF, u & 0xFF)


# ----------------------------------------------------------------------------- #
# CORE: read_cal(node) -- importable, GUI-reusable
# ----------------------------------------------------------------------------- #
def read_cal(node, model=None, ref_csv=None, save_log=True, node_id=None):
    """Read + audit all cal/config values off an already-connected canopen node.

    Returns a report dict:
      {node, model, product_code, sw_version, hw_version, entries[list of dict],
       suspect_count, healthy(bool), lines[list of str], log_path}
    Each entries[] item: {group,name,idx,sub,value,baseline,status,why,unit}.

    Does NOT write to the puck. save_log writes the text report to scripts/callogs/.
    """
    try:
        node.sdo.RESPONSE_TIMEOUT = 1.0
    except Exception:
        pass

    # --- identity up front (model drives which baseline template we load) ---
    product_code = None
    try:
        product_code = _read_int(node, 0x1018, 2, False)
    except _Absent:
        pass
    if not model:
        model = can_backend.model_from_product_code(product_code) if product_code is not None else None
    sw_u = None
    try:
        sw_u = _read_int(node, 0x100A, 0, False)
    except _Absent:
        pass
    sw_version = _decode_sw_version(sw_u) if sw_u is not None else "unknown"

    # Gate the bias-format check on the detected fw version (Q12.4 landed post-4.3.3).
    global _BIAS_Q124
    if sw_u is not None:
        _ver = ((sw_u >> 24) & 0xFF, (sw_u >> 8) & 0xFFFF, sw_u & 0xFF)
        _BIAS_Q124 = _ver >= (4, 4, 0)   # True: Q12.4 (>=4.4.0); False: raw ADC (<=4.3.3)
    else:
        _BIAS_Q124 = None                # unknown fw -> magnitude fallback in _chk_bias

    # --- baseline: --ref real config wins, else the model blank template ---
    base_path = ref_csv if ref_csv else baseline_path_for_model(model)
    baseline = load_baseline(base_path)

    # --- read + check every entry ---
    results = []
    suspect = 0
    for group, items in ENTRIES:
        for idx, sub, name, signed, kind, unit, checker in items:
            base = baseline.get((idx, sub))
            entry = {"group": group, "name": name, "idx": idx, "sub": sub,
                     "unit": unit, "baseline": base}
            try:
                if kind == "float":
                    value, uraw = _read_float(node, idx, sub)
                else:
                    value = _read_int(node, idx, sub, signed)
                    uraw = value
            except _Absent:
                entry.update(value=None, status=MISSING,
                             value_str="N/A (absent on this fw)", why="")
                results.append(entry)
                continue

            entry["value"] = value
            entry["value_str"] = _fmt_value(value, uraw, kind)
            if idx == 0x100A:                 # decode packed MAJOR.MINOR.PATCH
                entry["value_str"] = _decode_sw_version(value)

            if checker is not None:
                status, why = checker(value, base)
            elif base is not None and kind != "float":
                # generic: matches blank template -> DEFAULT (informational)
                status, why = (DEFAULT, "matches blank {}".format(base)) if value == base else (OK, "")
            else:
                status, why = OK, ""
            entry["status"], entry["why"] = status, why
            if status == SUSPECT:
                suspect += 1
            results.append(entry)

    healthy = suspect == 0

    # --- build the text report ---
    lines = []

    def emit(s=""):
        lines.append(s)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    emit("=" * 96)
    emit("PUCK4 CALIBRATION / CONFIG AUDIT  (READ-ONLY)")
    emit("  node: {}   model: {}   product_code: {}   fw: {}".format(
        node_id if node_id is not None else "?", model or "UNKNOWN",
        ("0x{:X}".format(product_code) if product_code is not None else "?"), sw_version))
    emit("  baseline: {}".format(base_path or "NONE (model not matched / no template)"))
    emit("  date: {}".format(stamp))
    emit("=" * 96)
    emit("  {:<24}{:<12}{:>24}{:>14}  {:<8} {}".format(
        "NAME", "INDEX:SUB", "VALUE", "BASELINE", "STATUS", "NOTE / WHY"))
    emit("-" * 96)
    cur_group = None
    for e in results:
        if e["group"] != cur_group:
            cur_group = e["group"]
            emit("[{}]".format(cur_group))
        idxsub = "0x{:04X}:{}".format(e["idx"], e["sub"])
        base_str = str(e["baseline"]) if e["baseline"] is not None else "-"
        note = e["why"]
        if e["unit"] and e["status"] != MISSING:
            note = ("{} ".format(e["unit"]) + note).strip()
        emit("  {:<24}{:<12}{:>24}{:>14}  {:<8} {}".format(
            e["name"][:24], idxsub, e["value_str"][:24], base_str[:14],
            e["status"], note))
    emit("-" * 96)

    # --- summary ---
    if healthy:
        emit("SUMMARY: cal looks healthy -- 0 SUSPECT values.")
    else:
        emit("SUMMARY: {} SUSPECT value(s) -- see above (possible cal damage / bad cal).".format(suspect))
        for e in results:
            if e["status"] == SUSPECT:
                emit("  !! {:<22} 0x{:04X}:{}  {}  -- {}".format(
                    e["name"][:22], e["idx"], e["sub"], e["value_str"], e["why"]))
    n_default = sum(1 for e in results if e["status"] == DEFAULT)
    n_missing = sum(1 for e in results if e["status"] == MISSING)
    emit("  ({} DEFAULT/uncalibrated, {} MISSING/absent-on-this-fw)".format(n_default, n_missing))
    emit("=" * 96)

    # --- save a timestamped copy ---
    log_path = None
    if save_log:
        logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "callogs")
        os.makedirs(logdir, exist_ok=True)
        fname = "readcal_{}_node{}_{}.log".format(
            model or "UNKNOWN", node_id if node_id is not None else "NA",
            time.strftime("%Y%m%d-%H%M%S"))
        log_path = os.path.join(logdir, fname)
        with open(log_path, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    return {"node": node_id, "model": model, "product_code": product_code,
            "sw_version": sw_version, "entries": results, "suspect_count": suspect,
            "healthy": healthy, "lines": lines, "log_path": log_path}


# ----------------------------------------------------------------------------- #
# CLI (thin wrapper: parse args, connect, call read_cal, print)
# ----------------------------------------------------------------------------- #
def _parse_argv(argv):
    model = ref = None
    can = node = None
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print("Usage: read_cal.py [--can can0] [--node 127] [--model P4-16] [--ref <config.csv>]\n"
                  "  Reads all cal/config values off a puck and flags anomalies vs a baseline.\n"
                  "  --ref <known-good.csv>  point at a REAL config to enable the R/L doubled-value check\n"
                  "                          (the blank templates carry only placeholder R/L).\n"
                  "  Positional [can] [node] also accepted (e.g. read_cal.py can0 127).")
            sys.exit(0)
        if a in ("--can", "--dev") and i + 1 < len(argv):
            can = argv[i + 1]; i += 2; continue
        if a.startswith("--can="):
            can = a.split("=", 1)[1]; i += 1; continue
        if a == "--node" and i + 1 < len(argv):
            node = int(argv[i + 1]); i += 2; continue
        if a.startswith("--node="):
            node = int(a.split("=", 1)[1]); i += 1; continue
        if a == "--model" and i + 1 < len(argv):
            model = argv[i + 1]; i += 2; continue
        if a.startswith("--model="):
            model = a.split("=", 1)[1]; i += 1; continue
        if a == "--ref" and i + 1 < len(argv):
            ref = argv[i + 1]; i += 2; continue
        if a.startswith("--ref="):
            ref = a.split("=", 1)[1]; i += 1; continue
        pos.append(a); i += 1
    if can is None:
        can = pos[0] if len(pos) > 0 else "can0"
    if node is None:
        node = int(pos[1]) if len(pos) > 1 else 127
    return can, node, model, ref


def main():
    can, node_id, model, ref = _parse_argv(sys.argv[1:])
    net = can_backend.make_network(can, bitrate=1_000_000)
    try:
        node = net.add_node(node_id, EDS)
        report = read_cal(node, model=model, ref_csv=ref, save_log=True, node_id=node_id)
        print("\n".join(report["lines"]))
        if report["log_path"]:
            print("\nSaved: {}".format(report["log_path"]))
        return 1 if report["suspect_count"] else 0
    finally:
        try:
            net.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
