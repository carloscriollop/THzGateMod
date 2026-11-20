"""
Run the TSP square-wave function under a real Lua interpreter (lupa) with
stub smua/smub/timer/digio objects. Checks the level sequence, the TTL line,
the readings per half period, the timing and the fault abort.
Run: python -m pytest tests  (or python tests/test_tsp_script.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lupa  # noqa: E402
from gatemod_core import TSP_SCRIPT  # noqa: E402

STUBS = r"""
clock = 0
events = {}
function make_buf()
  local b = {readings = {}, timestamps = {}, sourcevalues = {}, n = 0}
  function b.clear() b.readings = {}; b.timestamps = {}; b.sourcevalues = {}; b.n = 0 end
  return b
end
timer = {measure = {}}
function timer.reset() clock = 0 end
function timer.measure.t() clock = clock + 0.0005; return clock end
digio = {}
function digio.writebit(line, v) table.insert(events, {"dio", line, v, clock}) end
leak = 1e-10
function make_smu(name, cur)
  local s = {source = {}, nvbuffer1 = make_buf(), measure = {}}
  local store = {}
  local mt = {__index = store,
    __newindex = function(t, k, v)
      store[k] = v
      if k == "levelv" then table.insert(events, {name, v, clock}) end
    end}
  setmetatable(s.source, mt)
  function s.measure.v(buf)
    if buf.appendmode ~= 1 then buf.clear() end  -- 2600B default: overwrite
    buf.n = buf.n + 1
    buf.readings[buf.n] = 4.25
    buf.timestamps[buf.n] = clock
    buf.sourcevalues[buf.n] = 0
  end
  function s.measure.i(buf)
    if buf.appendmode ~= 1 then buf.clear() end  -- 2600B default: overwrite
    buf.n = buf.n + 1
    buf.readings[buf.n] = cur(s.source.levelv)
    buf.timestamps[buf.n] = clock
    buf.sourcevalues[buf.n] = s.source.levelv
  end
  return s
end
smua = make_smu("smua", function(v) return leak * (v or 0) end)
smub = make_smu("smub", function(v) return 5e-6 end)
printed = nil
function print(...) printed = {...} end
"""


def run(args, leak=1e-10):
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(STUBS)
    lua.execute(f"leak = {leak}")
    lua.execute(TSP_SCRIPT)
    lua.globals().gm_run(*args)
    g = lua.globals()
    printed = [g.printed[i] for i in range(1, len(g.printed) + 1)]
    ev = [list(e.values()) for e in g.events.values()]
    ba = g.smua.nvbuffer1
    n = ba.n
    rd = [ba.readings[i] for i in range(1, n + 1)]
    ts = [ba.timestamps[i] for i in range(1, n + 1)]
    sv = [ba.sourcevalues[i] for i in range(1, n + 1)]
    nb = g.smub.nvbuffer1.n
    return printed, ev, rd, ts, sv, nb


def test_completed_run():
    # vl, vh, half, ncyc, ifault, nviol, line, tmeas, measd
    printed, ev, rd, ts, sv, nb = run((-3, 4, 0.05, 5, 1e-7, 3, 2, 0.006, 1))
    assert printed == [0, 10], printed
    assert sv == [-3, 4] * 5
    assert nb == 10
    gate = [e[1] for e in ev if e[0] == "smua"]
    assert gate == [-3, 4] * 5 + [0], gate
    dio = [e[2] for e in ev if e[0] == "dio"]
    assert dio == [1, 0] * 5 + [0], dio
    # each reading lands in the last tmeas of its half period
    for k, t in enumerate(ts):
        end = (k + 1) * 0.05
        assert end - 0.006 <= t <= end + 1e-9, (k, t)
    print("completed run OK")


def test_fault_abort():
    # leak of 1e-7 A/V: |IG| = 3e-7 at -3 V, above ifault -> abort after 3 half periods
    printed, ev, rd, ts, sv, nb = run((-3, 4, 0.05, 5, 1e-7, 3, 0, 0.006, 0), leak=1e-7)
    assert printed == [1, 3], printed
    assert nb == 0
    gate = [e[1] for e in ev if e[0] == "smua"]
    assert gate[-1] == 0
    assert not [e for e in ev if e[0] == "dio"]
    print("fault abort OK")


def test_voltage_mode():
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(STUBS)
    lua.execute(TSP_SCRIPT)
    lua.globals().gm_run(-3, 4, 0.02, 3, 1e-7, 3, 0, 0.006, 2)
    bb = lua.globals().smub.nvbuffer1
    assert bb.n == 6 and bb.readings[1] == 4.25
    print("voltage mode OK")


def test_lua50_compatible():
    """The 2636B runs Lua 5.0: no % operator, no # length operator."""
    import re
    import gatemod_core as core
    code = "\n".join(l.split("--")[0] for l in core.TSP_SCRIPT.splitlines())
    assert "%" not in code, "Lua 5.0 has no % operator; use a toggle or math.mod"
    assert not re.search(r"#\w", code), "Lua 5.0 has no # length operator"


if __name__ == "__main__":
    test_voltage_mode()
    test_completed_run()
    test_fault_abort()
    test_lua50_compatible()

