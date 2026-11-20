"""
THzGateMod core: square-wave gate drive for a back-gated film on a Keithley
2636B (TSP mode), with a TTL reference for a lock-in amplifier.

SMU A (smua) drives the gate, SMU B (smub) holds VDS on the drain and reads
ID. The square wave itself runs inside the instrument as a TSP function, so
its timing does not depend on Python or on the VISA link. Python only loads
the script, starts a run, waits for it and reads the buffers afterwards.

Nothing here imports tkinter: the GUI (thz_gate_mod.py) and the offline
tests both use this module.
"""

import csv
import math
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

import numpy as np

APP_VERSION = "2026-11-20.2"

# ---------------------------------------------------------------------------
# TSP code loaded into the instrument.
#
# gm_run(vl, vh, half, ncyc, ifault, nviol, line, tmeas, measd)
#   vl, vh  : gate levels of the two half periods (V). The run starts on vl.
#   half    : half period (s).
#   ncyc    : number of full cycles.
#   ifault  : |IG| at the END of a half period above this counts as a fault (A).
#             At that point the gate capacitor has finished charging, so a
#             large current there is leakage, not charging.
#   nviol   : consecutive faulty half periods that abort the run.
#   line    : digital I/O line for the TTL reference (1..14), 0 = none.
#             The line is HIGH during the vl half period, so the lock-in
#             reference rising edge marks the start of each vl half.
#   tmeas   : time reserved before the end of each half period for the
#             readings (s).
#   measd   : what smub reads at the end of each half period:
#             0 = nothing, 1 = drain current (ID), 2 = voltage (lock-in output).
#
# Readings go to smua.nvbuffer1 (IG, with source values and timestamps) and,
# if measd > 0, smub.nvbuffer1 (ID or volts). One reading per channel per half period.
# Prints "status n": status 0 = completed, 1 = aborted on gate fault.
# The gate is left at 0 V and the TTL line low.
# ---------------------------------------------------------------------------
TSP_SCRIPT_NAME = "GateMod"
TSP_SCRIPT = r"""
function gm_run(vl, vh, half, ncyc, ifault, nviol, line, tmeas, measd)
  local ba = smua.nvbuffer1
  local bb = smub.nvbuffer1
  ba.clear()
  ba.collecttimestamps = 1
  ba.collectsourcevalues = 1
  bb.clear()
  bb.collecttimestamps = 1
  local viol = 0
  local status = 0
  local tnext = 0
  timer.reset()
  for k = 1, 2 * ncyc do
    local on = (k % 2 == 1)
    if on then smua.source.levelv = vl else smua.source.levelv = vh end
    if line > 0 then
      if on then digio.writebit(line, 1) else digio.writebit(line, 0) end
    end
    tnext = tnext + half
    while timer.measure.t() < tnext - tmeas do end
    smua.measure.i(ba)
    if measd == 1 then smub.measure.i(bb) elseif measd == 2 then smub.measure.v(bb) end
    if math.abs(ba.readings[ba.n]) > ifault then viol = viol + 1 else viol = 0 end
    if viol >= nviol then
      status = 1
      break
    end
    while timer.measure.t() < tnext do end
  end
  smua.source.levelv = 0
  if line > 0 then digio.writebit(line, 0) end
  print(status, ba.n)
end
"""


# ---------------------------------------------------------------------------
# Parameters and results
# ---------------------------------------------------------------------------

@dataclass
class SquareParams:
    v_on: float = -3.0          # gate level of the first half period (V)
    v_off: float = 4.0          # gate level of the second half period (V)
    vds: float = 0.1            # drain bias (V)
    comp_gate: float = 20e-6    # gate current limit during the square wave (A)
    comp_drain: float = 1e-3    # drain current limit (A)
    range_drain: float = 100e-6  # fixed drain current range (A)
    nplc: float = 0.05          # integration time per reading (power-line cycles)
    i_fault: float = 100e-9     # end-of-half-period |IG| that counts as a fault (A)
    n_viol: int = 3             # consecutive faulty half periods that abort
    r_series: float = 0.0       # resistor in series with the gate (ohm), for checks only
    c_gate: Optional[float] = None  # measured gate capacitance (F), for checks
    smub_mode: str = "drain"    # "drain" (ID), "lockin" (SR830 output voltage) or "off"
    lockin_vmax: float = 10.0   # full scale of the lock-in output (V)
    dio_line: int = 0           # 0 = no TTL reference
    line_freq: float = 60.0     # power-line frequency (Hz)
    ramp_step: float = 0.02
    ramp_delay: float = 0.05

    @property
    def dv(self):
        return abs(self.v_off - self.v_on)

    @property
    def measure_drain(self):
        """True when SMU B takes a reading (drain current or lock-in voltage)."""
        return self.smub_mode != "off"

    @property
    def measd_code(self):
        return {"off": 0, "drain": 1, "lockin": 2}[self.smub_mode]


@dataclass
class RunResult:
    freq: float
    half: float
    status: int                 # 0 completed, 1 gate fault, 2 stopped by user
    t: np.ndarray               # time of each reading (s)
    vset: np.ndarray            # programmed gate level of each reading (V)
    ig: np.ndarray              # gate current at the end of each half period (A)
    sb: Optional[np.ndarray]    # SMU B reading at the end of each half period (A or V)
    started: str = ""
    note: str = ""
    smub_mode: str = "drain"


@dataclass
class CycleStats:
    freq: float
    n_cycles_used: int
    i_on: float                 # mean ID at the end of the on half periods (A)
    i_off: float                # mean ID at the end of the off half periods (A)
    ratio: float                # i_on / i_off
    swing: float                # (i_on - i_off) / i_on
    ratio_first: float          # ratio over the first used cycles
    ratio_last: float           # ratio over the last used cycles
    ig_max: float               # max |IG| at the end of a half period (A)


# ---------------------------------------------------------------------------
# Checks done before a run
# ---------------------------------------------------------------------------

def t_meas(p: SquareParams) -> float:
    """Time reserved for the readings at the end of each half period (s)."""
    one = p.nplc / p.line_freq + 1e-3
    n = 2 if p.measure_drain else 1
    return 2.0 * n * one + 2e-3


def charge_time(p: SquareParams) -> Optional[float]:
    """
    Time for the gate to finish charging after a step (s), or None if C is
    unknown. The SMU delivers at most comp_gate, so the step first charges
    at constant current, then settles with the series RC.
    """
    if not p.c_gate:
        return None
    q = p.c_gate * p.dv
    return q / p.comp_gate + 5.0 * p.r_series * p.c_gate


def check_frequency(p: SquareParams, freq: float):
    """
    Return (ok, message). A frequency is refused when the gate cannot finish
    charging within 30% of the half period, or when the readings would not
    fit in the half period.
    """
    half = 0.5 / freq
    tm = t_meas(p)
    if half < 4 * tm:
        return False, (f"{freq:g} Hz: half period {half*1e3:.1f} ms is too short for the "
                       f"readings ({tm*1e3:.1f} ms). Lower NPLC or the frequency.")
    tc = charge_time(p)
    if tc is None:
        return True, f"{freq:g} Hz: gate C not measured, charging time not checked."
    if tc > 0.3 * half:
        need = p.c_gate * p.dv / max(0.3 * half - 5 * p.r_series * p.c_gate, 1e-12)
        hint = (f"raise the gate compliance to about {need:.2g} A" if 0.3 * half > 5 * p.r_series * p.c_gate
                else "use a smaller series resistor")
        return False, (f"{freq:g} Hz: the gate needs about {tc*1e3:.0f} ms to charge, more than "
                       f"30% of the {half*1e3:.0f} ms half period; {hint}.")
    return True, f"{freq:g} Hz: charging takes about {tc*1e3:.1f} ms of a {half*1e3:.0f} ms half period."


def suggest_comp(p: SquareParams, freq: float, margin: float = 2.0) -> Optional[float]:
    """Gate compliance that charges the gate within 30% of the half period."""
    if not p.c_gate:
        return None
    half = 0.5 / freq
    avail = 0.3 * half - 5 * p.r_series * p.c_gate
    if avail <= 0:
        return None
    return margin * p.c_gate * p.dv / avail


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def cycle_stats(res: RunResult, skip_cycles: int = 3) -> Optional[CycleStats]:
    """
    Modulation from the readings at the end of each half period. The first
    skip_cycles cycles are left out: right after the gate starts switching,
    the trapped charge has not settled into its periodic state yet.
    """
    if res.sb is None or len(res.sb) < 2:
        return None
    n_cyc = len(res.sb) // 2
    i_on = res.sb[0:2 * n_cyc:2]
    i_off = res.sb[1:2 * n_cyc:2]
    s = min(skip_cycles, max(n_cyc - 1, 0))
    i_on, i_off = i_on[s:], i_off[s:]
    if len(i_on) == 0:
        return None
    k = max(1, len(i_on) // 5)
    m_on, m_off = float(np.mean(i_on)), float(np.mean(i_off))
    return CycleStats(
        freq=res.freq, n_cycles_used=len(i_on), i_on=m_on, i_off=m_off,
        ratio=m_on / m_off if m_off else math.nan,
        swing=(m_on - m_off) / m_on if m_on else math.nan,
        ratio_first=float(np.mean(i_on[:k]) / np.mean(i_off[:k])),
        ratio_last=float(np.mean(i_on[-k:]) / np.mean(i_off[-k:])),
        ig_max=float(np.max(np.abs(res.ig))) if len(res.ig) else math.nan,
    )


@dataclass
class LockinStats:
    freq: float
    n_cycles_used: int
    x_on: float                 # mean lock-in output at the end of the on half periods (V)
    x_off: float                # mean lock-in output at the end of the off half periods (V)
    rel: float                  # (X on - X off) / X off, drift-corrected
    rel_err: float              # standard error of rel (one standard deviation of the mean)


def lockin_stats(res: RunResult, skip_cycles: int = 3) -> Optional[LockinStats]:
    """
    Relative change of the lock-in output between the two gate states.
    Each on reading is compared with the mean of the off readings just
    before and just after it, so a slow drift of the THz signal cancels.
    rel_err is the scatter of these per-cycle values divided by sqrt(N): it
    shrinks as the run gets longer.
    """
    x = res.sb
    if x is None or len(x) < 4:
        return None
    n_cyc = len(x) // 2
    x_on, x_off = x[0:2 * n_cyc:2], x[1:2 * n_cyc:2]
    s = max(1, min(skip_cycles, n_cyc - 2))
    ref = float(np.mean(x_off[s:]))
    if ref == 0:
        return None
    d = np.array([x_on[k] - 0.5 * (x_off[k - 1] + x_off[k]) for k in range(s, n_cyc)]) / ref
    err = float(np.std(d, ddof=1) / np.sqrt(len(d))) if len(d) > 1 else math.nan
    return LockinStats(freq=res.freq, n_cycles_used=len(d), x_on=float(np.mean(x_on[s:])),
                       x_off=ref, rel=float(np.mean(d)), rel_err=err)


def capacitance_from_ramps(t_up, v_up, t_dn, v_dn, i0):
    """
    C from a constant-current charge and discharge: slope_up = (I0 - IL)/C,
    slope_dn = -(I0 + IL)/C, so C = 2 I0 / (slope_up - slope_dn) and a
    leakage IL that is the same in both directions cancels.
    Fits only the middle part of each ramp (20% to 80% of the excursion).
    """
    def mid_slope(t, v):
        t, v = np.asarray(t, float), np.asarray(v, float)
        lo, hi = np.min(v), np.max(v)
        m = (v >= lo + 0.2 * (hi - lo)) & (v <= lo + 0.8 * (hi - lo))
        if m.sum() < 3:
            m = np.ones_like(v, bool)
        return np.polyfit(t[m], v[m], 1)[0]
    s_up, s_dn = mid_slope(t_up, v_up), mid_slope(t_dn, v_dn)
    if s_up - s_dn <= 0:
        raise ValueError("Gate voltage did not ramp up and back down; check the wiring.")
    c = 2 * i0 / (s_up - s_dn)
    i_leak = i0 - c * s_up
    return c, s_up, s_dn, i_leak


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def header_lines(p: SquareParams, res: RunResult, mode: str):
    d = asdict(p)
    return [
        f"# THzGateMod v{APP_VERSION}, Keithley 2636B TSP",
        f"# Mode: {mode}",
        f"# Started: {res.started}",
        f"# Square wave: v_on={p.v_on} V (first half, TTL high), v_off={p.v_off} V, "
        f"f={res.freq:g} Hz, half={res.half:g} s",
        "# " + ", ".join(f"{k}={d[k]}" for k in
                         ("vds", "comp_gate", "comp_drain", "range_drain", "nplc", "i_fault",
                          "n_viol", "r_series", "c_gate", "smub_mode", "lockin_vmax", "dio_line")),
        f"# Status: {['completed', 'aborted on gate fault', 'stopped by user'][res.status]}"
        + (f" ({res.note})" if res.note else ""),
        "# One row per half period, read at its end. Phase: on = v_on half, off = v_off half.",
    ]


def write_run_csv(path, p: SquareParams, res: RunResult, mode: str):
    with open(path, "w", newline="") as fh:
        for line in header_lines(p, res, mode):
            fh.write(line + "\n")
        w = csv.writer(fh)
        col = {"drain": "ID_A", "lockin": "LockinOut_V"}.get(res.smub_mode, "SMUB")
        w.writerow(["Index", "Cycle", "Phase", "Time_s", "VG_set_V", "IG_A", col])
        for k in range(len(res.t)):
            idv = "" if res.sb is None else f"{res.sb[k]:.7e}"
            w.writerow([k, k // 2, "on" if k % 2 == 0 else "off", f"{res.t[k]:.6f}",
                        f"{res.vset[k]:.6g}", f"{res.ig[k]:.6e}", idv])


def write_summary(path, p: SquareParams, stats: List[CycleStats], notes: List[str]):
    lines = [f"THzGateMod v{APP_VERSION}  square-wave modulation summary",
             f"v_on = {p.v_on} V, v_off = {p.v_off} V, VDS = {p.vds} V, "
             f"series R = {p.r_series:g} ohm, gate C = "
             + (f"{p.c_gate*1e9:.1f} nF" if p.c_gate else "not measured"),
             "",
             f"{'f (Hz)':>8} {'cycles':>7} {'ID on (uA)':>11} {'ID off (uA)':>12} {'on/off':>7} "
             f"{'swing %':>8} {'first':>7} {'last':>7} {'max|IG| (nA)':>13}"]
    for s in stats:
        lines.append(f"{s.freq:>8g} {s.n_cycles_used:>7d} {s.i_on*1e6:>11.4f} {s.i_off*1e6:>12.4f} "
                     f"{s.ratio:>7.3f} {s.swing*100:>8.1f} {s.ratio_first:>7.3f} {s.ratio_last:>7.3f} "
                     f"{s.ig_max*1e9:>13.2f}")
    lines += ["", "first / last: on/off over the first and last fifth of the cycles used.",
              "If last < first, the modulation is still decaying at that frequency."]
    if notes:
        lines += ["", "Notes:"] + [f"  {n}" for n in notes]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------

class K2636BGateMod:
    """Square-wave gate drive on a 2636B. smua = gate, smub = drain."""

    def __init__(self, inst):
        self.k = inst
        self.k.read_termination = "\n"
        self.k.write_termination = "\n"
        self._loaded = False

    # --- low level ---
    def _w(self, cmd):
        self.k.write(cmd)

    def _q(self, cmd):
        return self.k.query(cmd).strip()

    def model(self):
        return self._q("print(localnode.model)")

    def drain_errors(self, max_n=20):
        errs = []
        try:
            n = int(float(self._q("print(errorqueue.count)")))
        except Exception:
            return errs
        for _ in range(min(n, max_n)):
            try:
                errs.append(self._q("local c, m = errorqueue.next() print(c, m)"))
            except Exception:
                break
        return errs

    def load_script(self):
        """Load the TSP functions. Each line of the script is sent on its own."""
        self._w("if abort ~= nil then abort() end")
        self._w("errorqueue.clear()")
        self._w(f"loadscript {TSP_SCRIPT_NAME}")
        for line in TSP_SCRIPT.strip().splitlines():
            self._w(line)
        self._w("endscript")
        self._w(f"{TSP_SCRIPT_NAME}()")
        errs = self.drain_errors()
        if errs:
            raise RuntimeError("The instrument rejected the square-wave script:\n" + "\n".join(errs))
        self._loaded = True

    def _level(self, node):
        return float(self._q(f"print({node}.source.levelv)"))

    def ramp(self, node, target, step, delay):
        try:
            v0 = self._level(node)
        except Exception:
            v0 = 0.0
        n = int(max(1, math.ceil(abs(target - v0) / max(abs(step), 1e-3))))
        for i in range(1, n + 1):
            self._w(f"{node}.source.levelv = {v0 + (target - v0) * i / n}")
            time.sleep(delay)

    # --- setup ---
    def init_square(self, p: SquareParams):
        """
        Voltage source on both channels, FIXED current ranges and autozero once,
        so every reading of a run takes the same time. Outputs on, drain ramped
        to VDS, gate at 0 V.
        """
        if not self._loaded:
            self.load_script()
        w = self._w
        w("smua.reset()")
        w("smua.source.func = smua.OUTPUT_DCVOLTS")
        w("smua.source.autorangev = smua.AUTORANGE_OFF")
        w(f"smua.source.rangev = {max(abs(p.v_on), abs(p.v_off))}")
        w(f"smua.source.limiti = {p.comp_gate}")
        w("smua.measure.autorangei = smua.AUTORANGE_OFF")
        w(f"smua.measure.rangei = {p.comp_gate}")
        w(f"smua.measure.nplc = {p.nplc}")
        w("smua.measure.autozero = smua.AUTOZERO_ONCE")
        w("smua.sense = smua.SENSE_LOCAL")
        w("smua.source.levelv = 0")
        w("smub.reset()")
        if p.smub_mode == "lockin":
            # Voltmeter: source 0 A, voltage limit above the lock-in full scale,
            # fixed voltage range. The SMU draws no current from the lock-in output.
            w("smub.source.func = smub.OUTPUT_DCAMPS")
            w("smub.source.leveli = 0")
            w(f"smub.source.limitv = {1.5 * p.lockin_vmax}")
            w("smub.measure.autorangev = smub.AUTORANGE_OFF")
            w(f"smub.measure.rangev = {p.lockin_vmax}")
        else:
            w("smub.source.func = smub.OUTPUT_DCVOLTS")
            w("smub.source.autorangev = smub.AUTORANGE_ON")
            w(f"smub.source.limiti = {p.comp_drain}")
            w("smub.measure.autorangei = smub.AUTORANGE_OFF")
            w(f"smub.measure.rangei = {p.range_drain}")
            w("smub.source.levelv = 0")
        w(f"smub.measure.nplc = {p.nplc}")
        w("smub.measure.autozero = smub.AUTOZERO_ONCE")
        w("smub.sense = smub.SENSE_LOCAL")
        if p.dio_line:
            w(f"digio.writebit({int(p.dio_line)}, 0)")
        errs = self.drain_errors()
        if errs:
            raise RuntimeError("Configuration errors, outputs left OFF:\n" + "\n".join(errs))
        w("smua.source.output = smua.OUTPUT_ON")
        if p.smub_mode == "drain":
            w("smub.source.output = smub.OUTPUT_ON")
            self.ramp("smub", p.vds, p.ramp_step, p.ramp_delay)
        elif p.smub_mode == "lockin":
            w("smub.source.output = smub.OUTPUT_ON")

    # --- capacitance ---
    def measure_capacitance(self, i0=20e-9, vmax=1.0, timeout=60.0, poll=0.05, stop=None):
        """
        Constant current I0 into the gate up to vmax, then -I0 back to near 0,
        reading the gate voltage with timestamps. Voltage limit = vmax, so the
        gate never sees more than vmax. Returns (C, details).
        """
        w = self._w
        if not self._loaded:
            self.load_script()
        w("smua.reset()")
        w("smua.source.func = smua.OUTPUT_DCAMPS")
        w(f"smua.source.limitv = {vmax * 1.05}")
        w("smua.source.leveli = 0")
        w("smua.measure.autorangev = smua.AUTORANGE_ON")
        w("smua.measure.nplc = 0.1")
        w("smua.source.output = smua.OUTPUT_ON")
        self._q("timer.reset() print(0)")

        def run(level, until):
            w(f"smua.source.leveli = {level}")
            ts, vs = [], []
            t_start = time.time()
            while time.time() - t_start < timeout:
                if stop is not None and stop():
                    break
                s = self._q("print(timer.measure.t(), smua.measure.v())").replace(",", " ").split()
                t, v = float(s[0]), float(s[1])
                ts.append(t)
                vs.append(v)
                if until(v):
                    break
                time.sleep(poll)
            return ts, vs

        try:
            t_up, v_up = run(i0, lambda v: v >= 0.9 * vmax)
            t_dn, v_dn = run(-i0, lambda v: v <= 0.1 * vmax)
        finally:
            w("smua.source.leveli = 0")
            w("smua.source.output = smua.OUTPUT_OFF")
            w("smua.source.func = smua.OUTPUT_DCVOLTS")
            w("smua.source.levelv = 0")
        c, s_up, s_dn, il = capacitance_from_ramps(t_up, v_up, t_dn, v_dn, i0)
        return c, dict(t_up=t_up, v_up=v_up, t_dn=t_dn, v_dn=v_dn,
                       slope_up=s_up, slope_dn=s_dn, i_leak=il, i0=i0)

    # --- square wave ---
    def run_square(self, p: SquareParams, freq: float, n_cycles: int) -> RunResult:
        """
        Run n_cycles of the square wave inside the instrument and return the
        readings. Blocks until the run ends (the VISA timeout is extended to
        cover it). abort() from another thread stops it.
        """
        half = 0.5 / freq
        dur = 2 * n_cycles * half
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        old_to = self.k.timeout
        self.k.timeout = int((dur + 60) * 1000)
        status, note = 0, ""
        try:
            ans = self._q(f"gm_run({p.v_on}, {p.v_off}, {half}, {int(n_cycles)}, {p.i_fault}, "
                          f"{int(p.n_viol)}, {int(p.dio_line)}, {t_meas(p)}, "
                          f"{p.measd_code})")
            parts = ans.replace(",", " ").split()
            status = int(float(parts[0]))
        except Exception as e:
            status, note = 2, f"run interrupted: {e}"
        finally:
            self.k.timeout = old_to
        if status == 2:
            self.after_abort(p)
        t, vset, ig, idr = self.read_buffers(p.measure_drain)
        if status == 1:
            note = (f"|IG| above {p.i_fault:g} A at the end of {p.n_viol} consecutive half periods")
        return RunResult(freq=freq, half=half, status=status, t=t, vset=vset, ig=ig,
                         sb=idr, started=started, note=note, smub_mode=p.smub_mode)

    def read_buffers(self, measure_drain, chunk=500):
        n = int(float(self._q("print(smua.nvbuffer1.n)")))
        t, vs, ig, idr = [], [], [], []
        for a in range(1, n + 1, chunk):
            b = min(n, a + chunk - 1)
            cols = ("smua.nvbuffer1.timestamps, smua.nvbuffer1.sourcevalues, smua.nvbuffer1.readings"
                    + (", smub.nvbuffer1.readings" if measure_drain else ""))
            vals = [float(x) for x in self._q(f"printbuffer({a}, {b}, {cols})").replace(",", " ").split()]
            k = 4 if measure_drain else 3
            for j in range(0, len(vals) - k + 1, k):
                t.append(vals[j])
                vs.append(vals[j + 1])
                ig.append(vals[j + 2])
                if measure_drain:
                    idr.append(vals[j + 3])
        return (np.array(t), np.array(vs), np.array(ig),
                np.array(idr) if measure_drain else None)

    def abort(self):
        """Stop a running gm_run. A device clear aborts the running script."""
        try:
            self.k.clear()
        except Exception:
            pass

    def after_abort(self, p: SquareParams):
        for cmd in ("smua.source.levelv = 0",
                    f"digio.writebit({int(p.dio_line)}, 0)" if p.dio_line else None):
            if cmd:
                try:
                    self._w(cmd)
                except Exception:
                    pass

    def zero_and_off(self, p: SquareParams):
        for node in ("smua",) if p.smub_mode == "lockin" else ("smua", "smub"):
            try:
                self.ramp(node, 0.0, p.ramp_step, p.ramp_delay)
            except Exception:
                pass
        for cmd in ("smua.source.output = smua.OUTPUT_OFF", "smub.source.output = smub.OUTPUT_OFF"):
            try:
                self._w(cmd)
            except Exception:
                pass
        if p.dio_line:
            try:
                self._w(f"digio.writebit({int(p.dio_line)}, 0)")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Simulated instrument (no hardware). Same methods as K2636BGateMod.
# ---------------------------------------------------------------------------

class SimulatedGateMod:
    """
    A film whose gate effect is compensated by charge that relaxes in
    minutes, as in sample 4 on 20 Nov 2026. ID = I0 - g * (VG - Vt), where the
    compensating voltage Vt follows VG with time constant tau. Numbers are
    illustrative, not a fit.
    """

    def __init__(self, c_gate=120e-9, i0=7.6e-6, g=0.70e-6, tau=300.0, leak_r=1.5e9, seed=0):
        self.c_gate, self.i0, self.g, self.tau, self.leak_r = c_gate, i0, g, tau, leak_r
        self.vt = 0.0
        self.rng = np.random.default_rng(seed)
        self._stop = False
        self.fault_after = None  # simulate a gate fault after this many half periods

    def model(self):
        return "2636B (simulated)"

    def init_square(self, p):
        self._stop = False

    def measure_capacitance(self, i0=20e-9, vmax=1.0, timeout=60.0, poll=0.05, stop=None):
        il = 0.5 / self.leak_r
        s_up, s_dn = (i0 - il) / self.c_gate, -(i0 + il) / self.c_gate
        t_up = np.arange(0, 0.9 * vmax / s_up, poll)
        t_dn = np.arange(0, 0.8 * vmax / -s_dn, poll)
        v_up = s_up * t_up + self.rng.normal(0, 1e-4, t_up.size)
        v_dn = 0.9 * vmax + s_dn * t_dn + self.rng.normal(0, 1e-4, t_dn.size)
        c, a, b, l = capacitance_from_ramps(t_up, v_up, t_dn, v_dn, i0)
        return c, dict(t_up=t_up, v_up=v_up, t_dn=t_dn, v_dn=v_dn, slope_up=a, slope_dn=b,
                       i_leak=l, i0=i0)

    def run_square(self, p, freq, n_cycles):
        half = 0.5 / freq
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        t, vs, ig, idr = [], [], [], []
        status, note, viol = 0, "", 0
        for k in range(2 * n_cycles):
            if self._stop:
                status, note = 2, "stopped by user"
                break
            v = p.v_on if k % 2 == 0 else p.v_off
            self.vt += (v - self.vt) * (1 - math.exp(-half / self.tau))
            tk = (k + 1) * half - t_meas(p) / 2
            i_g = v / self.leak_r + self.rng.normal(0, 5e-11)
            if self.fault_after is not None and k >= self.fault_after:
                i_g = 1e-6
            i_d = self.i0 - self.g * (v - self.vt) + self.rng.normal(0, 2e-9)
            if p.smub_mode == "lockin":
                # THz signal near 5 V full scale: 0.3 % larger in the on state, slow drift, noise
                i_d = 5.0 * (1 + 0.003 * (k % 2 == 0) + 2e-4 * tk / 60) + self.rng.normal(0, 0.01)
            t.append(tk)
            vs.append(v)
            ig.append(i_g)
            idr.append(i_d)
            viol = viol + 1 if abs(i_g) > p.i_fault else 0
            if viol >= p.n_viol:
                status, note = 1, f"|IG| above {p.i_fault:g} A at the end of {p.n_viol} consecutive half periods"
                break
        self.vt *= math.exp(-2.0 / self.tau)
        return RunResult(freq=freq, half=half, status=status, t=np.array(t), vset=np.array(vs),
                         ig=np.array(ig), sb=np.array(idr) if p.measure_drain else None,
                         started=started, note=note, smub_mode=p.smub_mode)

    def abort(self):
        self._stop = True

    def zero_and_off(self, p):
        pass
