"""End-to-end checks of the core with the simulated instrument (no hardware)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gatemod_core as core  # noqa: E402


def test_capacitance():
    dev = core.SimulatedGateMod(c_gate=120e-9)
    c, d = dev.measure_capacitance(i0=20e-9, vmax=1.0)
    assert abs(c - 120e-9) / 120e-9 < 0.02, c
    print(f"C = {c*1e9:.1f} nF, leak {d['i_leak']*1e9:.2f} nA")


def test_checks():
    p = core.SquareParams(c_gate=120e-9, comp_gate=20e-6, r_series=1e6)
    ok01, m01 = core.check_frequency(p, 0.1)
    ok10, m10 = core.check_frequency(p, 10)
    assert ok01 and not ok10, (m01, m10)
    p2 = core.SquareParams(c_gate=120e-9, comp_gate=20e-6, r_series=1e3)
    assert core.check_frequency(p2, 1)[0]
    ok, m = core.check_frequency(p2, 10)
    sug = core.suggest_comp(p2, 10)
    print("1 MOhm:", m01, "|", m10)
    print("1 kOhm at 10 Hz:", m, "suggested", sug)


def test_dc_sequence_and_files():
    dev = core.SimulatedGateMod()
    p = core.SquareParams(c_gate=120e-9, r_series=1e3)
    dev.init_square(p)
    stats = []
    with tempfile.TemporaryDirectory() as d:
        for f in (0.01, 0.1, 1, 10):
            res = dev.run_square(p, f, 20)
            assert res.status == 0 and len(res.t) == 40
            core.write_run_csv(os.path.join(d, f"run_{f}.csv"), p, res, "DC test")
            stats.append(core.cycle_stats(res, 3))
        core.write_summary(os.path.join(d, "summary.txt"), p, stats, [])
        txt = open(os.path.join(d, "summary.txt")).read()
        print(txt)
        rows = open(os.path.join(d, "run_1.csv")).read().splitlines()
        assert rows[7].startswith("Index,Cycle,Phase"), rows[:9]
    r = [s.ratio for s in stats]
    assert r[0] < r[1] < r[2] <= r[3] + 0.01, r


def test_fault():
    dev = core.SimulatedGateMod()
    dev.fault_after = 6
    p = core.SquareParams()
    res = dev.run_square(p, 1, 20)
    assert res.status == 1 and len(res.t) == 9, (res.status, len(res.t))
    print("fault:", res.note)


if __name__ == "__main__":
    test_capacitance()
    test_checks()
    test_dc_sequence_and_files()
    test_fault()
    print("all OK")
