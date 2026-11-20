# Changelog

## 2026-11-20.4

- Fix: the instrument rejected the TSP script ("')' expected near '%'").
  The 2636B runs Lua 5.0, which has no % operator; the on/off phase is now a
  toggle. New test checks the script for Lua 5.1-only operators.

## 2026-11-20.3

- THz run default frequency 0.25 Hz, which fits the SR830 time constant the
  THz setup normally uses (100 ms, 24 dB/oct). README explains the settling
  rule and how to check it on the first run.

## 2026-11-20.2

- THz run: SMU B can read the SR830 X output as a voltmeter (source 0 A,
  fixed 10 V range) at the end of every half period, so the existing 50 kHz
  antenna reference stays untouched. New result: (X on - X off) / X off with
  its standard error, each on reading compared with the mean of the
  neighbouring off readings (cancels slow drift).
- TSP gm_run: measd = 2 reads voltage on smub.
- "SMU B reads" selector in the THz tab (SR830 output, drain current, nothing).

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
