# THzGateMod - square-wave gate drive for terahertz modulation experiments
#
# Keithley 2636B in TSP mode. SMU A drives the gate with a square wave
# between two levels; SMU B holds VDS and reads ID (optional). A digital I/O
# line gives the lock-in (SR830) a TTL reference that is HIGH during the
# first ("on") half period.
#
# Two uses:
#   DC test  - run the square wave at several frequencies on the DC bench and
#              measure how much ID modulation survives at each one.
#   THz run  - the same square wave for a set time while the terahertz setup
#              and the lock-in measure the change of the transmitted field.
#
# The square wave runs inside the instrument (gatemod_core.TSP_SCRIPT), so
# its timing does not depend on this program. See README.md for wiring.
#
# Requires: pyvisa, numpy, matplotlib, tkinter

import os
import re
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import gatemod_core as core

APP_TITLE = f"THzGateMod - square-wave gate drive, Keithley 2636B   v{core.APP_VERSION}"
SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gatemod_settings.json")
BLUE, ORANGE, GRAY = "#2A5AA8", "#D0612E", "#8A8F97"


def safe_name(text, fallback="sample"):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip())
    return s or fallback


def parse_freqs(text):
    out = []
    for tok in re.split(r"[,\s;]+", text.strip()):
        if tok:
            f = float(tok)
            if f <= 0:
                raise ValueError("Frequencies must be positive.")
            out.append(f)
    if not out:
        raise ValueError("Enter at least one frequency.")
    return out


class App(tk.Tk):
    SMUB_MODES = {"SR830 output (V)": "lockin", "Drain current (A)": "drain", "Nothing": "off"}

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1320x880")
        self.q = queue.Queue()
        self.rm = None
        self.k = None
        self.dev = None
        self.worker = None
        self.stop_flag = threading.Event()
        self.settings = self._load_settings()
        self._build_ui()
        self.after(50, self._poll)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ===== settings =====
    def _load_settings(self):
        try:
            with open(SETTINGS) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_settings(self):
        s = {k: v.get() for k, v in self.vars.items()}
        s["measure_drain_b"] = bool(self.var_drain.get())
        try:
            with open(SETTINGS, "w") as fh:
                json.dump(s, fh, indent=1)
        except Exception:
            pass

    # ===== UI =====
    def _var(self, key, default):
        v = tk.StringVar(value=str(self.settings.get(key, default)))
        self.vars[key] = v
        return v

    def _build_ui(self):
        self.vars = {}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        left = ttk.Frame(root)
        left.pack(side="left", fill="y")
        right = ttk.Frame(root)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        def section(title):
            f = ttk.LabelFrame(left, text=title, padding=6)
            f.pack(fill="x", pady=(0, 6))
            return f

        def entry(parent, label, key, default, row, col, width=11):
            ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(0, 4), pady=2)
            e = ttk.Entry(parent, textvariable=self._var(key, default), width=width)
            e.grid(row=row, column=col + 1, sticky="w", padx=(0, 10), pady=2)
            return e

        # 1. connection
        c = section("1. Connection")
        self.var_res = self._var("resource", "")
        self.cmb = ttk.Combobox(c, textvariable=self.var_res, width=34)
        self.cmb.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(c, text="Detect", command=self.on_detect).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Button(c, text="Connect", command=self.on_connect).grid(row=1, column=1, sticky="w", pady=4)
        self.var_sim = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="Simulate (no instrument)", variable=self.var_sim,
                        command=self.on_sim_toggle).grid(row=1, column=2, sticky="w")
        self.lbl_conn = ttk.Label(c, text="Not connected.")
        self.lbl_conn.grid(row=2, column=0, columnspan=3, sticky="w")

        # 2. sample
        s = section("2. Sample and data folder")
        entry(s, "Sample", "sample", "sample4-hf02", 0, 0, 24)
        self.var_dir = self._var("data_dir", os.path.join(os.path.expanduser("~"), "THzGateMod_data"))
        ttk.Entry(s, textvariable=self.var_dir, width=30).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Button(s, text="Browse", command=self.on_browse).grid(row=1, column=2, sticky="w", padx=4)

        # 3. square wave and safety
        w = section("3. Square wave and gate safety")
        entry(w, "V on (V)", "v_on", -3.0, 0, 0)
        entry(w, "V off (V)", "v_off", 4.0, 0, 2)
        entry(w, "Gate compliance (A)", "comp_gate", 2e-5, 1, 0)
        entry(w, "Series R (ohm)", "r_series", 1e6, 1, 2)
        entry(w, "Fault |IG| (A)", "i_fault", 1e-7, 2, 0)
        entry(w, "Faults to abort", "n_viol", 3, 2, 2)
        entry(w, "NPLC", "nplc", 0.05, 3, 0)
        entry(w, "Line freq (Hz)", "line_freq", 60, 3, 2)
        entry(w, "VDS (V)", "vds", 0.1, 4, 0)
        entry(w, "Drain range (A)", "range_drain", 1e-4, 4, 2)
        self.var_drain = tk.BooleanVar(value=bool(self.settings.get("measure_drain_b", True)))
        ttk.Checkbutton(w, text="Drain wired: read ID on SMU B", variable=self.var_drain).grid(
            row=5, column=0, columnspan=4, sticky="w")
        ttk.Label(w, text="Fault = |IG| above the limit at the END of a half period, when the\n"
                          "gate has finished charging. The compliance only caps the charging current.",
                  foreground="#6b7280").grid(row=6, column=0, columnspan=4, sticky="w")

        # 4. capacitance
        cap = section("4. Gate capacitance")
        entry(cap, "Test current (A)", "c_i0", 2e-8, 0, 0)
        entry(cap, "Up to (V)", "c_vmax", 1.0, 0, 2)
        entry(cap, "C (nF)", "c_nf", "", 1, 0)
        ttk.Button(cap, text="Measure C", command=self.on_measure_c).grid(row=1, column=2, sticky="w")
        ttk.Label(cap, text="Measure once per sample, or type a value. Needed for the\n"
                            "charging-time check of each frequency.",
                  foreground="#6b7280").grid(row=2, column=0, columnspan=4, sticky="w")

        # 5. tabs
        nb = ttk.Notebook(left)
        nb.pack(fill="x", pady=(0, 6))
        dc = ttk.Frame(nb, padding=6)
        thz = ttk.Frame(nb, padding=6)
        nb.add(dc, text="DC test")
        nb.add(thz, text="THz run")
        entry(dc, "Frequencies (Hz)", "freqs", "0.1, 1, 10", 0, 0, 16)
        entry(dc, "Cycles per frequency", "n_cycles", 20, 1, 0)
        entry(dc, "Skip first cycles", "skip", 3, 2, 0)
        bf = ttk.Frame(dc)
        bf.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(bf, text="Check frequencies", command=self.on_check).pack(side="left")
        ttk.Button(bf, text="Start DC test", command=self.on_start_dc).pack(side="left", padx=6)
        entry(thz, "Frequency (Hz)", "thz_f", 2.0, 0, 0)
        entry(thz, "Duration (s)", "thz_dur", 600, 1, 0)
        entry(thz, "TTL line (1-14)", "dio_line", 1, 2, 0)
        ttk.Label(thz, text="SMU B reads").grid(row=3, column=0, sticky="w", pady=2)
        self.var_smub = self._var("smub_mode", "SR830 output (V)")
        ttk.Combobox(thz, textvariable=self.var_smub, values=list(self.SMUB_MODES), width=18,
                     state="readonly").grid(row=3, column=1, sticky="w")
        entry(thz, "SR830 output full scale (V)", "lockin_vmax", 10, 4, 0)
        ttk.Label(thz, text="SR830 stays on the 50 kHz antenna reference with a short time\n"
                            "constant; its CH1 OUTPUT (X) goes to SMU B, which reads it at the end\n"
                            "of every half period. The TTL line is for an oscilloscope.",
                  foreground="#6b7280").grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Button(thz, text="Start THz run", command=self.on_start_thz).grid(row=6, column=0, sticky="w",
                                                                              pady=(6, 0))
        ttk.Button(left, text="Stop", command=self.on_stop).pack(fill="x")
        self.var_status = tk.StringVar(value="Ready.")
        ttk.Label(left, textvariable=self.var_status, wraplength=420).pack(fill="x", pady=(6, 0))

        # right: plots and log
        self.fig = Figure(figsize=(8, 6.2), dpi=100)
        self.ax1 = self.fig.add_subplot(211)
        self.ax2 = self.fig.add_subplot(212)
        self.fig.tight_layout(pad=2.5)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.log = tk.Text(right, height=9, wrap="word")
        self.log.pack(fill="x", pady=(6, 0))

    # ===== thread-safe UI helpers =====
    def _post(self, fn):
        self.q.put(fn)

    def _poll(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(50, self._poll)

    def _say(self, msg):
        def f():
            self.log.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
            self.log.see("end")
        self._post(f)

    def _status(self, msg):
        self._post(lambda: self.var_status.set(msg))

    def _busy(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A measurement is running. Stop it first.")
            return True
        return False

    # ===== parameters =====
    def params(self, dio=False):
        g = lambda k: float(self.vars[k].get())  # noqa: E731
        c_txt = self.vars["c_nf"].get().strip()
        p = core.SquareParams(
            v_on=g("v_on"), v_off=g("v_off"), vds=g("vds"), comp_gate=g("comp_gate"),
            range_drain=g("range_drain"), nplc=g("nplc"), i_fault=g("i_fault"),
            n_viol=int(g("n_viol")), r_series=g("r_series"),
            c_gate=float(c_txt) * 1e-9 if c_txt else None,
            smub_mode=(self.SMUB_MODES[self.var_smub.get()] if dio
                       else ("drain" if self.var_drain.get() else "off")),
            lockin_vmax=g("lockin_vmax"),
            dio_line=int(g("dio_line")) if dio else 0, line_freq=g("line_freq"))
        if p.v_on == p.v_off:
            raise ValueError("V on and V off are equal.")
        if max(abs(p.v_on), abs(p.v_off)) > 10:
            raise ValueError("Gate levels above 10 V are refused by this program.")
        if p.i_fault >= p.comp_gate:
            raise ValueError("Fault |IG| must be below the gate compliance.")
        if dio and not 1 <= p.dio_line <= 14:
            raise ValueError("TTL line must be 1 to 14.")
        return p

    def _outdir(self):
        d = self.var_dir.get().strip()
        os.makedirs(d, exist_ok=True)
        return d

    def _stem(self, what):
        return os.path.join(self._outdir(), f"{safe_name(self.vars['sample'].get())}_{what}_"
                                            f"{time.strftime('%Y%m%d_%H%M%S')}")

    # ===== connection =====
    def on_sim_toggle(self):
        if self.var_sim.get():
            self.dev = core.SimulatedGateMod()
            self.lbl_conn.config(text="Simulated instrument (no hardware).")
        else:
            self.dev = None
            self.lbl_conn.config(text="Not connected.")

    def _rm(self):
        if self.rm is None:
            import pyvisa
            self.rm = pyvisa.ResourceManager()
        return self.rm

    def on_detect(self):
        try:
            res = list(self._rm().list_resources())
        except Exception as e:
            messagebox.showerror("VISA", str(e))
            return
        self.cmb["values"] = res
        if res and not self.var_res.get():
            self.var_res.set(res[0])
        self.var_status.set(f"{len(res)} VISA resource(s).")

    def on_connect(self):
        if self._busy():
            return
        self.var_sim.set(False)
        try:
            if self.k is not None:
                self.k.close()
            self.k = self._rm().open_resource(self.var_res.get().strip(), timeout=20000)
            self.dev = core.K2636BGateMod(self.k)
            model = self.dev.model()
            self.dev.load_script()
            self.lbl_conn.config(text=f"Connected: {model}, script loaded.")
        except Exception as e:
            self.dev = None
            messagebox.showerror("Connect", f"Could not connect or load the script:\n{e}")

    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or os.path.expanduser("~"))
        if d:
            self.var_dir.set(d)

    # ===== checks =====
    def on_check(self):
        try:
            p = self.params()
            freqs = parse_freqs(self.vars["freqs"].get())
        except Exception as e:
            messagebox.showerror("Parameters", str(e))
            return
        lines = []
        for f in freqs:
            ok, msg = core.check_frequency(p, f)
            sug = core.suggest_comp(p, f)
            lines.append(("OK   " if ok else "NO   ") + msg
                         + (f"  Suggested compliance: {sug:.2g} A." if sug and not ok else ""))
        for ln in lines:
            self._say(ln)
        messagebox.showinfo("Frequency check", "\n\n".join(lines))

    # ===== capacitance =====
    def on_measure_c(self):
        if self._busy() or not self._need_dev():
            return
        try:
            i0 = float(self.vars["c_i0"].get())
            vmax = float(self.vars["c_vmax"].get())
            p = self.params()
        except Exception as e:
            messagebox.showerror("Parameters", str(e))
            return
        if not (0 < vmax <= 2.0):
            messagebox.showerror("Parameters", "Keep the capacitance test at or below 2 V.")
            return
        self.stop_flag.clear()

        def work():
            self._status("Measuring gate capacitance...")
            try:
                c, d = self.dev.measure_capacitance(i0=i0, vmax=vmax, stop=self.stop_flag.is_set)
            except Exception as e:
                self._say(f"Capacitance measurement failed: {e}")
                self._status("Capacitance measurement failed.")
                return
            finally:
                try:
                    self.dev.zero_and_off(p)
                except Exception:
                    pass
            path = self._stem("gateC") + ".csv"
            with open(path, "w") as fh:
                fh.write(f"# THzGateMod v{core.APP_VERSION} gate capacitance, I0={i0} A, up to {vmax} V\n")
                fh.write(f"# C = {c:.4e} F, slope up {d['slope_up']:.4g} V/s, slope down "
                         f"{d['slope_dn']:.4g} V/s, leakage {d['i_leak']:.3g} A\n")
                fh.write("Segment,Time_s,VG_V\n")
                for seg in ("up", "dn"):
                    for t, v in zip(d[f"t_{seg}"], d[f"v_{seg}"]):
                        fh.write(f"{seg},{t:.4f},{v:.6f}\n")

            def show():
                self.vars["c_nf"].set(f"{c*1e9:.1f}")
                self.ax1.clear()
                self.ax1.plot(d["t_up"], d["v_up"], ".", color=BLUE, label="+I0")
                self.ax1.plot(np.asarray(d["t_dn"]), d["v_dn"], ".", color=ORANGE, label="-I0")
                self.ax1.set_xlabel("time (s)")
                self.ax1.set_ylabel("VG (V)")
                self.ax1.set_title(f"Gate capacitance: {c*1e9:.1f} nF")
                self.ax1.legend()
                self.canvas.draw_idle()
            self._post(show)
            self._say(f"Gate C = {c*1e9:.1f} nF (leakage during the test {d['i_leak']*1e9:.2f} nA). "
                      f"Saved {os.path.basename(path)}")
            self._status(f"Gate C = {c*1e9:.1f} nF.")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _need_dev(self):
        if self.dev is None:
            messagebox.showwarning("Not connected", "Connect to the 2636B or tick Simulate.")
            return False
        return True

    # ===== DC test =====
    def on_start_dc(self):
        if self._busy() or not self._need_dev():
            return
        try:
            p = self.params()
            freqs = parse_freqs(self.vars["freqs"].get())
            n_cyc = int(float(self.vars["n_cycles"].get()))
            skip = int(float(self.vars["skip"].get()))
        except Exception as e:
            messagebox.showerror("Parameters", str(e))
            return
        if p.smub_mode != "drain":
            messagebox.showerror("DC test", "The DC test needs the drain wired (ID on SMU B).")
            return
        bad = [m for ok, m in (core.check_frequency(p, f) for f in freqs) if not ok]
        if bad:
            messagebox.showerror("Frequency check", "\n\n".join(bad))
            return
        if p.c_gate is None and not messagebox.askyesno(
                "Gate C unknown", "The charging time cannot be checked without C. Run anyway?"):
            return
        total = sum(2 * n_cyc * 0.5 / f for f in freqs)
        self._save_settings()
        self.stop_flag.clear()
        self._say(f"DC test: {', '.join(f'{f:g}' for f in freqs)} Hz, {n_cyc} cycles each, "
                  f"about {total/60:.1f} min of square wave.")
        self.worker = threading.Thread(target=self._dc_worker, args=(p, freqs, n_cyc, skip), daemon=True)
        self.worker.start()

    def _dc_worker(self, p, freqs, n_cyc, skip):
        stats, notes = [], []
        stem = self._stem("sqw")
        try:
            self.dev.init_square(p)
            for f in freqs:
                if self.stop_flag.is_set():
                    break
                self._status(f"Square wave at {f:g} Hz ({n_cyc} cycles, {n_cyc/f:.0f} s)...")
                res = self.dev.run_square(p, f, n_cyc)
                path = f"{stem}_{f:g}Hz.csv"
                core.write_run_csv(path, p, res, "DC test")
                st = core.cycle_stats(res, skip)
                if st:
                    stats.append(st)
                    self._say(f"{f:g} Hz: on/off {st.ratio:.3f} (first {st.ratio_first:.3f}, last "
                              f"{st.ratio_last:.3f}), max |IG| {st.ig_max*1e9:.1f} nA. "
                              f"Saved {os.path.basename(path)}")
                self._post(lambda r=res, s=list(stats): self._plot_dc(r, s))
                if res.status == 1:
                    notes.append(f"Aborted at {f:g} Hz: {res.note}.")
                    self._say("GATE FAULT: " + res.note + ". Stopping the test.")
                    break
                if res.status == 2:
                    notes.append(f"Stopped by user at {f:g} Hz.")
                    break
        except Exception as e:
            self._say(f"Error: {e}")
            notes.append(f"Error: {e}")
        finally:
            try:
                self.dev.zero_and_off(p)
            except Exception:
                pass
        if stats:
            core.write_summary(stem + "_summary.txt", p, stats, notes)
            self._say(f"Summary saved: {os.path.basename(stem)}_summary.txt")
        self._status("DC test finished." if not notes else "DC test ended: " + notes[-1])

    def _plot_dc(self, res, stats):
        self.ax1.clear()
        if res.sb is not None and len(res.t):
            on = np.arange(len(res.t)) % 2 == 0
            self.ax1.plot(res.t[on], res.sb[on] * 1e6, "o-", ms=3, color=BLUE, label="end of V on")
            self.ax1.plot(res.t[~on], res.sb[~on] * 1e6, "s-", ms=3, color=ORANGE, label="end of V off")
            self.ax1.set_xlabel("time (s)")
            self.ax1.set_ylabel("ID (uA)")
            self.ax1.set_title(f"{res.freq:g} Hz")
            self.ax1.legend(fontsize=8)
        self.ax2.clear()
        if stats:
            f = [s.freq for s in stats]
            self.ax2.semilogx(f, [s.ratio for s in stats], "o-", color=BLUE, label="on/off")
            self.ax2.semilogx(f, [s.ratio_last for s in stats], "s--", color=GRAY, label="last fifth")
            self.ax2.set_xlabel("square-wave frequency (Hz)")
            self.ax2.set_ylabel("ID on / ID off")
            self.ax2.legend(fontsize=8)
        self.fig.tight_layout(pad=2.5)
        self.canvas.draw_idle()

    # ===== THz run =====
    def on_start_thz(self):
        if self._busy() or not self._need_dev():
            return
        try:
            p = self.params(dio=True)
            f = float(self.vars["thz_f"].get())
            dur = float(self.vars["thz_dur"].get())
        except Exception as e:
            messagebox.showerror("Parameters", str(e))
            return
        ok, msg = core.check_frequency(p, f)
        if not ok:
            messagebox.showerror("Frequency check", msg)
            return
        n_cyc = max(1, int(round(dur * f)))
        if not messagebox.askokcancel(
                "THz run", f"Square wave {p.v_on:g} / {p.v_off:g} V at {f:g} Hz for {n_cyc/f:.0f} s, "
                           f"TTL on digital I/O line {p.dio_line}. SMU B reads: {self.var_smub.get()}.\n\n"
                           f"The program waits until the run ends; Stop aborts it."):
            return
        self._save_settings()
        self.stop_flag.clear()
        self.worker = threading.Thread(target=self._thz_worker, args=(p, f, n_cyc), daemon=True)
        self.worker.start()

    def _thz_worker(self, p, f, n_cyc):
        stem = self._stem("thzrun") + f"_{f:g}Hz"
        try:
            self.dev.init_square(p)
            self._status(f"THz run: {f:g} Hz until {time.strftime('%H:%M:%S', time.localtime(time.time() + n_cyc / f))}...")
            res = self.dev.run_square(p, f, n_cyc)
            core.write_run_csv(stem + ".csv", p, res, "THz run")
            if p.smub_mode == "lockin":
                ls = core.lockin_stats(res, 3)
                extra = (f"; lock-in (X on - X off)/X off = {ls.rel*100:.3f} % +/- {ls.rel_err*100:.3f} % "
                         f"over {ls.n_cycles_used} cycles" if ls else "")
            else:
                st = core.cycle_stats(res, 3)
                extra = f"; ID on/off during the run {st.ratio:.3f}" if st else ""
                ls = None
            self._post(lambda: self._plot_thz(res, ls))
            msg = ["completed", "ABORTED on gate fault: " + res.note, "stopped by user"][res.status]
            self._say(f"THz run {msg}. Saved {os.path.basename(stem)}.csv" + extra)
        except Exception as e:
            self._say(f"Error: {e}")
        finally:
            try:
                self.dev.zero_and_off(p)
            except Exception:
                pass
        self._status("THz run finished.")

    def _plot_thz(self, res, ls=None):
        self.ax1.clear()
        on = np.arange(len(res.t)) % 2 == 0
        if res.sb is not None:
            k, unit = (1.0, "lock-in output (V)") if res.smub_mode == "lockin" else (1e6, "ID (uA)")
            self.ax1.plot(res.t[on], res.sb[on] * k, ".", ms=2, color=BLUE, label="end of V on")
            self.ax1.plot(res.t[~on], res.sb[~on] * k, ".", ms=2, color=ORANGE, label="end of V off")
            self.ax1.set_ylabel(unit)
            self.ax1.legend(fontsize=8)
        title = f"THz run, {res.freq:g} Hz"
        if ls:
            title += f":  dX/X = {ls.rel*100:.3f} % +/- {ls.rel_err*100:.3f} %"
        self.ax1.set_title(title)
        self.ax2.clear()
        self.ax2.plot(res.t, np.abs(res.ig) * 1e9, ".", ms=2, color=GRAY)
        self.ax2.set_xlabel("time (s)")
        self.ax2.set_ylabel("|IG| at end of half period (nA)")
        self.fig.tight_layout(pad=2.5)
        self.canvas.draw_idle()

    # ===== stop / close =====
    def on_stop(self):
        self.stop_flag.set()
        if self.dev is not None:
            self.dev.abort()
        self.var_status.set("Stopping...")

    def on_close(self):
        self._save_settings()
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quit", "A run is active. Stop it and quit?"):
                return
            self.on_stop()
            self.worker.join(timeout=10)
        try:
            if self.k is not None:
                self.k.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
