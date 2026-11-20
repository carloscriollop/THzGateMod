# Changelog

## 2026-11-20.1

First version.

- Square wave on the gate (SMU A) between two levels, run inside the 2636B as
  a TSP function (`gm_run`), with one reading of IG and ID at the end of every
  half period and a TTL reference on a digital I/O line (high during V on).
- Gate fault check at the end of each half period, when the gate capacitor
  has finished charging; three consecutive faults abort and set the gate to 0 V.
- Fixed current ranges and autozero once during runs, so every half period
  takes the same time.
- Gate capacitance from a constant-current charge and discharge (leakage
  cancels between the two slopes).
- Frequency check: refuses frequencies where the gate cannot charge within
  30% of the half period for the measured C, series R and compliance.
- DC test (several frequencies, on/off per frequency, summary file) and THz
  run (one frequency for a set time).
- Simulated instrument and offline tests: TSP function under a Lua
  interpreter with stubs, driver against a fake VISA session, GUI logic with
  tkinter stubs. Not yet run on hardware.
