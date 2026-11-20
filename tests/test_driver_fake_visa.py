"""
K2636BGateMod against a fake VISA session: checks the commands sent (script
loading, fixed ranges, outputs) and the parsing of gm_run and printbuffer
answers, including the interleaved printbuffer order (index by index).
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gatemod_core as core  # noqa: E402


class FakeVisa:
    def __init__(self, n=6):
        self.writes, self.timeout, self.n = [], 20000, n
        self.read_termination = self.write_termination = None

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        if cmd.startswith("print(errorqueue.count)"):
            return "0"
        if cmd.startswith("print(smub.source.levelv)") or cmd.startswith("print(smua.source.levelv)"):
            return "0"
        if cmd.startswith("gm_run("):
            return f"0\t{self.n}"
        if cmd == "print(smua.nvbuffer1.n)":
            return f"{self.n}"
        m = re.match(r"printbuffer\((\d+), (\d+), (.*)\)", cmd)
        if m:
            a, b, cols = int(m.group(1)), int(m.group(2)), m.group(3).split(", ")
            vals = []
            for i in range(a, b + 1):
                row = {"smua.nvbuffer1.timestamps": 0.05 * i,
                       "smua.nvbuffer1.sourcevalues": -3 if i % 2 else 4,
                       "smua.nvbuffer1.readings": 1e-10 * i,
                       "smub.nvbuffer1.readings": (9e-6 if i % 2 else 5e-6)}
                vals += [f"{row[c]:.6e}" for c in cols]
            return ", ".join(vals)
        if cmd.startswith("print(localnode.model)"):
            return "2636B"
        raise AssertionError("unexpected query " + cmd)


def test_driver():
    fv = FakeVisa(n=6)
    dev = core.K2636BGateMod(fv)
    p = core.SquareParams(dio_line=2, ramp_delay=0)
    dev.init_square(p)
    w = fv.writes
    assert "loadscript GateMod" in w and "endscript" in w and "GateMod()" in w
    assert any(x.startswith("function gm_run(") for x in w)
    assert "smua.measure.autorangei = smua.AUTORANGE_OFF" in w
    assert f"smua.source.limiti = {p.comp_gate}" in w
    assert w.index("smua.source.output = smua.OUTPUT_ON") > w.index("endscript")
    res = dev.run_square(p, 10, 3)
    assert res.status == 0 and len(res.t) == 6
    assert list(res.vset) == [-3, 4, -3, 4, -3, 4]
    assert abs(res.idrain[0] - 9e-6) < 1e-12 and abs(res.idrain[1] - 5e-6) < 1e-12
    run_cmd = [x for x in w if x.startswith("gm_run(")][0]
    assert ", 2, " in run_cmd, run_cmd           # TTL line passed
    assert fv.timeout == 20000                    # timeout restored
    st = core.cycle_stats(res, 0)
    assert abs(st.ratio - 1.8) < 1e-9
    print("driver OK:", run_cmd)


if __name__ == "__main__":
    test_driver()
