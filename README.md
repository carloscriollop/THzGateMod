# THzGateMod

Square-wave gate drive for terahertz modulation experiments on back-gated
films, with a Keithley 2636B in TSP mode and an SR830 lock-in.

- SMU A (smua) drives the gate with a square wave between **V on** and
  **V off** (default −3 V and +4 V).
- SMU B (smub) holds VDS on the drain and reads ID (optional).
- A digital I/O line of the 2636B gives the lock-in a TTL reference, HIGH
  during the V on half period.

The square wave runs inside the instrument as a TSP function
(`gatemod_core.TSP_SCRIPT`), so its timing does not depend on Python or on
the VISA link. Separate from DCforGating on purpose: different job, different
safety rules, different setup.

**Not yet run on hardware.** The TSP function was tested under a Lua
interpreter with stub instruments, and the program logic with a simulated
instrument and a fake VISA session (`tests/`). Do the first-use checks below
before connecting a sample.

## Files

- `thz_gate_mod.py`: the GUI.
- `gatemod_core.py`: instrument control, TSP script, checks, analysis, files,
  simulated instrument. No GUI code.
- `tests/`: offline tests (`python tests/test_tsp_script.py`, etc.).

## Setup

```
pip install -r requirements.txt
python thz_gate_mod.py
```

Needs a VISA backend (NI-VISA or Keysight IO Libraries) and the 2636B in TSP
command mode. Tick **Simulate** to try the program without an instrument.

## Wiring

| From | To |
|---|---|
| SMU A HI | series resistor, then the gate (Si back contact) |
| SMU A LO | source (common) |
| SMU B HI | drain |
| SMU B LO | source (common) |
| Digital I/O pin N (N = TTL line, 1 to 14) | BNC center, to SR830 REF IN |
| Digital I/O ground (any of pins 15 to 21) | BNC shield |

Do not connect pin 24 (interlock) or the +5 V pins (22, 23, 25). The
digital I/O lines are open-drain outputs at 0 or +5 V, which the 1 MΩ REF
IN of the SR830 reads as TTL.

## The gate is a capacitor

Each switch moves Q = C·ΔV of charge (about 0.7 µC for C = 100 nF and 7 V).
The gate compliance must let that charge in within part of the half period,
so it is set much higher than for DC sweeps. Protection works differently:

- **Fault check:** at the **end** of each half period, when charging is over,
  |IG| above **Fault |IG|** (default 100 nA) counts as a fault; three in a row
  abort the run and set the gate to 0 V.
- **Series resistor:** limits the current if the oxide fails, but slows the
  charging (time constant R·C).
  - 1 MΩ: a fault cannot pass more than about 4 µA. With C = 100 nF, R·C =
    0.1 s, so only frequencies up to about 0.3 Hz.
  - 1 kΩ: no speed limit, but almost no protection; the compliance is the
    only current limit.
  - 100 kΩ, if available, is a useful middle: R·C = 10 ms for 100 nF, fine up
    to a few Hz, fault current under 40 µA.
- **Check frequencies** computes, from the measured C, R and the compliance,
  whether the gate finishes charging within 30% of each half period, and
  refuses frequencies where it does not.

## Use

1. Connect (Detect, Connect). The script is loaded on connect.
2. Sample name and data folder.
3. Square wave levels, gate compliance, series R, fault limit.
4. **Measure C** once per sample: a constant current (default 20 nA) charges
   the gate to 1 V and back, and C comes from the two slopes (leakage
   cancels). Or type C in nF.
5. **DC test** tab: frequencies (default 0.1, 1, 10 Hz), cycles per frequency
   and cycles to skip. Check, then Start. For each frequency: a CSV with ID and
   IG at the end of every half period, and at the end a `_summary.txt` with
   on/off at each frequency. The first and last fifth of the cycles are
   compared, to see whether the modulation is still decaying.
6. **THz run** tab: one frequency, a duration and the TTL line. The program
   waits for the run to end; **Stop** aborts it (device clear). If Stop does
   not respond, press EXIT on the 2636B.

Files: `<sample>_sqw_<date>_<time>_<f>Hz.csv`, `..._summary.txt`,
`<sample>_thzrun_<date>_<time>_<f>Hz.csv`, `<sample>_gateC_<date>_<time>.csv`.

## SR830 settings for the THz run

- Reference: Source External (INTERNAL LED off), Trig POS EDGE.
- Time constant: about 10 periods or more (1 Hz: 10 to 30 s; 10 Hz: 1 to 3 s),
  slope 24 dB/oct, sync filter on (below 200 Hz).
- Phase: the rising edge marks the start of the V on half period. With the
  phase zeroed on a real signal, X > 0 means the transmitted field is larger
  during V on.
- Square wave to amplitude: the lock-in reads the rms of the fundamental. A
  square wave of peak-to-peak ΔE gives X = √2·ΔE/π, so **ΔE = π·X/√2 ≈ 2.22·X**.

With one SR830, the simplest scheme is the photoconductive antenna on DC
bias and the lock-in referenced to the gate TTL: X then measures the change of
the field directly. E itself (for ΔE/E) is measured separately, with the usual
modulated-antenna scan.

## First-use checks (no sample)

1. Connect: no error when the script loads.
2. Dummy gate: a 100 nF capacitor between SMU A HI (through the series
   resistor) and LO. Measure C: should read about 100 nF.
3. THz run at 1 Hz for 30 s with the drain unticked, TTL on line 1, an
   oscilloscope on the gate and on the TTL: square wave between V on and V off,
   TTL high during V on.
4. Press Stop in the middle of a run: the gate must go to 0 V within about a
   second.
5. Fault test: replace the capacitor by a 10 MΩ resistor (|IG| = 0.3 to 0.4 µA,
   above the 100 nA limit): the run must abort after three half periods.
