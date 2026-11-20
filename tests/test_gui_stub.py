"""
Drive the GUI logic without a display: tkinter and the Tk canvas are replaced
by small stubs, the instrument by the simulator. Checks parameter parsing, the
frequency check, the DC test and THz workers, file output and the plots.
"""
import os
import sys
import tempfile
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class Var:
    def __init__(self, value=None, master=None):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class Widget(mock.MagicMock):
    def __init__(self, *a, **k):
        super().__init__()


tk = types.ModuleType("tkinter")
tk.Tk = type("Tk", (), {"__init__": lambda self, *a, **k: None, "title": lambda *a: None,
                         "geometry": lambda *a: None, "after": lambda *a: None,
                         "protocol": lambda *a: None, "destroy": lambda *a: None})
tk.StringVar = Var
tk.BooleanVar = Var
tk.Text = Widget
ttk = types.ModuleType("tkinter.ttk")
for n in ("Frame", "LabelFrame", "Label", "Entry", "Button", "Combobox", "Checkbutton", "Notebook"):
    setattr(ttk, n, Widget)
mb = types.ModuleType("tkinter.messagebox")
mb.showinfo = mb.showerror = mb.showwarning = lambda *a, **k: None
mb.askyesno = mb.askokcancel = lambda *a, **k: True
fd = types.ModuleType("tkinter.filedialog")
tk.ttk, tk.messagebox, tk.filedialog = ttk, mb, fd
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb, "tkinter.filedialog": fd})
bt = types.ModuleType("matplotlib.backends.backend_tkagg")
bt.FigureCanvasTkAgg = lambda fig, master=None: mock.MagicMock()
sys.modules["matplotlib.backends.backend_tkagg"] = bt
import matplotlib  # noqa: E402
matplotlib.use = lambda *a, **k: None

import thz_gate_mod as gui  # noqa: E402


def make_app(tmp):
    with mock.patch.object(gui, "SETTINGS", os.path.join(tmp, "s.json")):
        app = gui.App()
    app.var_dir.set(tmp)
    app.var_sim.set(True)
    app.on_sim_toggle()
    app._post = lambda fn: fn()          # run UI updates inline
    return app


def test_dc_and_thz():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        app.vars["freqs"].set("0.1, 1")
        app.vars["n_cycles"].set("6")
        app.vars["c_nf"].set("120")
        app.vars["r_series"].set("1000")
        app.on_check()
        p = app.params()
        app._dc_worker(p, [0.1, 1.0], 6, 2)
        app.vars["thz_f"].set("2")
        app.vars["thz_dur"].set("5")
        p2 = app.params(dio=True)
        assert p2.dio_line == 1
        app._thz_worker(p2, 2.0, 10)
        files = sorted(os.listdir(tmp))
        print(files)
        assert any(f.endswith("_summary.txt") for f in files)
        assert sum(f.endswith("Hz.csv") for f in files) == 3
        log = "".join(str(c) for c in app.log.insert.call_args_list)
        assert "on/off" in log and "THz run completed" in log, log


def test_capacitance_button():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        started = []

        class T:
            def __init__(self, target, daemon=True):
                self.t = target

            def start(self):
                started.append(1)
                self.t()

            def is_alive(self):
                return False
        with mock.patch.object(gui.threading, "Thread", T):
            app.on_measure_c()
        assert started and abs(float(app.vars["c_nf"].get()) - 120) < 3, app.vars["c_nf"].get()
        assert any("gateC" in f for f in os.listdir(tmp))


def test_param_guards():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        app.vars["v_off"].set("12")
        try:
            app.params()
            raise AssertionError("should refuse 12 V")
        except ValueError as e:
            print("refused:", e)


if __name__ == "__main__":
    test_dc_and_thz()
    test_capacitance_button()
    test_param_guards()
    print("GUI logic OK")
