#!/usr/bin/env python3
"""
app.py — SIDOR (AK Protocol) Logger

A small desktop application that:
  - Connects to a SICK SIDOR gas analyzer over its "limited AK protocol"
    RS-232 interface (SIDOR Operating Instructions 8010939 V2.3, ch. 10).
  - Lets you pick the serial port and the instrument's AK-ID character.
  - Reads the list of measuring components from the instrument (AKMP) and
    lets you choose which one to monitor.
  - Lets you pick the sampling frequency.
  - As soon as you connect, starts polling automatically: shows the most
    recent reading and updates a live historical chart.
  - The chart's time window is independently selectable, and its x-axis
    shows each sample's timestamp as DDMMYYYY HH:MM:SS.
  - Logs every reading to a CSV file whose name you choose.

Requires: pyserial, matplotlib   (pip install pyserial matplotlib)
Tkinter is part of the Python standard library (on Debian/Ubuntu you may
need to install it separately: sudo apt install python3-tk).
"""

from __future__ import annotations

import csv
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import Optional

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.dates as mdates

try:
    from .ak_protocol import (
        SidorAnalyzer,
        SidorProtocolError,
        SidorNoMeasuringValue,
        list_available_ports,
    )
except ImportError:
    # allow running as a plain script (python app.py) as well as a module
    from ak_protocol import (
        SidorAnalyzer,
        SidorProtocolError,
        SidorNoMeasuringValue,
        list_available_ports,
    )


# ---------------------------------------------------------------------------
# Support contact info shown at the bottom of the window.
# Edit these two lines with the real contact details.
# ---------------------------------------------------------------------------
SUPPORT_CONTACT_NAME = "B. Lazarov"
SUPPORT_CONTACT_PHONE = "VITO"

# Path to the logo shown in the window header. Replace assets/logo.png with
# your own image (PNG) to use a real logo — no code changes needed.
LOGO_FILENAME = "vito.png"


def _resource_path(relative_path: str) -> Path:
    """Resolve a path to a bundled resource, both when running from source
    and when frozen into a onefile executable by PyInstaller (which unpacks
    bundled data to a temp folder referenced by sys._MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


class SidorLoggerApp(tk.Tk):
    # Display window options for the historical plot: label -> seconds
    # (None means "show everything collected so far").
    PLOT_WINDOWS = {
        "Last 1 min": 60,
        "Last 5 min": 5 * 60,
        "Last 15 min": 15 * 60,
        "Last 30 min": 30 * 60,
        "Last 1 hour": 60 * 60,
        "Last 4 hours": 4 * 60 * 60,
        "Last 24 hours": 24 * 60 * 60,
        "All data": None,
    }

    def __init__(self):
        super().__init__()
        self.title("SIDOR Logger")
        self.geometry("960x750")
        self.minsize(960, 660)

        # --- state -----------------------------------------------------
        self.analyzer: Optional[SidorAnalyzer] = None
        self.polling = False
        self.logging = False
        self.poll_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.data_queue: queue.Queue = queue.Queue()

        self.timestamps: list[datetime] = []
        self.values: list[float] = []
        self.csv_path: Optional[str] = None

        # thread-safe mirrors of Tk variables the poll thread needs to read
        # (Tcl/Tk variables are not safe to touch off the main thread)
        self._current_interval = 10.0
        self._current_component = 1

        # --- build UI ----------------------------------------------------
        self._build_header()
        self._build_connection_panel()
        self._build_logging_panel()
        self._build_value_panel()
        self._build_plot_panel()
        self._build_status_bar()

        self._refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # periodic GUI-side queue drain
        self.after(200, self._drain_queue)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_header(self):
        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=8, pady=(8, 0))

        logo_path = _resource_path(f"assets/{LOGO_FILENAME}")
        self._logo_image = None  # keep a reference so Tk doesn't garbage-collect it
        self._logo_warning = None
        if logo_path.exists():
            try:
                image = tk.PhotoImage(file=str(logo_path))
                max_dim = max(image.width(), image.height())
                target = 96
                if max_dim > target:
                    factor = max(1, max_dim // target)
                    image = image.subsample(factor, factor)
                self._logo_image = image
                ttk.Label(frame, image=self._logo_image).pack(side="right", padx=(0, 8))
            except tk.TclError as exc:
                self._logo_warning = f"Logo found at {logo_path} but could not be read: {exc}"
        else:
            self._logo_warning = (
                f"Logo not found at expected path: {logo_path} "
                f"(if this is a built .exe, it likely wasn't bundled — "
                f"rebuild with the --add-data flag in build_windows.bat)"
            )

        if self._logo_warning:
            print("WARNING:", self._logo_warning)

        ttk.Label(frame, text="SIDOR Logger", font=("TkDefaultFont", 14, "bold")).pack(side="left")

    def _build_connection_panel(self):
        frame = ttk.LabelFrame(self, text="Connection")
        frame.pack(fill="x", padx=8, pady=6)

        # --- row 0: port, AK-ID, connect ---------------------------------
        ttk.Label(frame, text="Serial port:").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, width=18, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=4, pady=4)

        ttk.Button(frame, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, padx=4)

        ttk.Label(frame, text="9600 baud, 8N1 (fixed)", foreground="#555").grid(
            row=0, column=3, padx=(12, 4), sticky="w"
        )

        ttk.Label(frame, text="AK-ID:").grid(row=0, column=4, padx=(12, 4), sticky="w")
        self.ak_id_var = tk.StringVar(value="A")
        ak_id_entry = ttk.Entry(frame, textvariable=self.ak_id_var, width=3)
        ak_id_entry.grid(row=0, column=5, padx=4)

        self.connect_btn = ttk.Button(frame, text="Connect", command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=6, padx=8)

        # --- row 1: measuring component ----------------------------------
        ttk.Label(frame, text="CO₂ [ppm]:").grid(row=1, column=0, padx=4, pady=(0, 6), sticky="w")
        self.component_var = tk.StringVar(value="1")
        self.component_combo = ttk.Combobox(
            frame, textvariable=self.component_var, width=28, state="disabled",
            values=["1", "2", "3", "4", "5"],
        )
        self.component_combo.grid(row=1, column=1, columnspan=3, padx=4, pady=(0, 6), sticky="w")
        self.component_combo.bind("<<ComboboxSelected>>", self._on_component_change)

        self.info_btn = ttk.Button(frame, text="Instrument info...", command=self._show_instrument_info, state="disabled")
        self.info_btn.grid(row=1, column=6, padx=8, pady=(0, 6))

    def _build_logging_panel(self):
        frame = ttk.LabelFrame(self, text="Logging")
        frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(frame, text="Sampling interval (s):").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.interval_var = tk.DoubleVar(value=10.0)

        def _on_interval_change(*_args):
            try:
                value = float(self.interval_var.get())
                if value > 0:
                    self._current_interval = value
            except (tk.TclError, ValueError):
                pass  # ignore transient invalid states while the user is typing

        self.interval_var.trace_add("write", _on_interval_change)

        interval_spin = ttk.Spinbox(
            frame, from_=1, to=3600, increment=1, textvariable=self.interval_var, width=8
        )
        interval_spin.grid(row=0, column=1, padx=4)

        ttk.Label(frame, text="CSV file:").grid(row=0, column=2, padx=(16, 4), sticky="w")
        self.csv_path_var = tk.StringVar(value="(none selected)")
        ttk.Label(frame, textvariable=self.csv_path_var, width=40, anchor="w").grid(
            row=0, column=3, padx=4, sticky="w"
        )
        ttk.Button(frame, text="Choose...", command=self._choose_csv_file).grid(row=0, column=4, padx=4)

        self.start_btn = ttk.Button(frame, text="Start logging", command=self._toggle_logging, state="disabled")
        self.start_btn.grid(row=0, column=5, padx=8)

    def _build_value_panel(self):
        frame = ttk.LabelFrame(self, text="Current reading")
        frame.pack(fill="x", padx=8, pady=6)

        self.value_var = tk.StringVar(value="--")
        value_label = ttk.Label(frame, textvariable=self.value_var, font=("TkDefaultFont", 32, "bold"))
        value_label.pack(side="left", padx=16, pady=8)

        self.timestamp_var = tk.StringVar(value="No reading yet")
        ttk.Label(frame, textvariable=self.timestamp_var).pack(side="left", padx=16)

    def _build_plot_panel(self):
        frame = ttk.LabelFrame(self, text="Historical data")
        frame.pack(fill="both", expand=True, padx=8, pady=6)

        controls = ttk.Frame(frame)
        controls.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(controls, text="Show:").pack(side="left", padx=(0, 4))
        self.plot_window_var = tk.StringVar(value="Last 15 min")
        window_combo = ttk.Combobox(
            controls, textvariable=self.plot_window_var, width=14, state="readonly",
            values=list(self.PLOT_WINDOWS.keys()),
        )
        window_combo.pack(side="left")
        window_combo.bind("<<ComboboxSelected>>", lambda e: self._update_plot())

        self.fig = Figure(figsize=(5, 3), dpi=100)
        self.ax = self.fig.add_subplot(111)
        # self.ax.set_xlabel("Date/time")
        self.ax.set_ylabel("CO₂ [ppm]")
        self.line, = self.ax.plot([], [], marker="o", markersize=3, linewidth=1)
        # x-axis tick labels as DDMMYY HH:MM, e.g. "240826 14:15".
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%d%m%y %H:%M"))
        self.fig.autofmt_xdate()

        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _build_status_bar(self):
        bar = ttk.Frame(self, relief="sunken")
        bar.pack(fill="x", side="bottom")

        initial_status = "Not connected."
        if getattr(self, "_logo_warning", None):
            initial_status = f"Not connected. (Note: {self._logo_warning})"

        self.status_var = tk.StringVar(value=initial_status)
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=4, pady=2
        )

        contact_text = f"Support: {SUPPORT_CONTACT_NAME} — {SUPPORT_CONTACT_PHONE}"
        ttk.Label(bar, text=contact_text, anchor="e", font=("TkDefaultFont", 8)).pack(
            side="right", padx=6, pady=2
        )

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    def _refresh_ports(self):
        ports = list_available_ports()
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connection(self):
        if self.analyzer is not None and self.analyzer.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("No port", "Please select a serial port first.")
            return

        ak_id = self.ak_id_var.get()
        if len(ak_id) != 1:
            messagebox.showerror("Invalid AK-ID", "AK-ID must be exactly one character.")
            return

        self.analyzer = SidorAnalyzer(port=port, ak_id=ak_id, timeout=2.0)
        try:
            self.analyzer.connect()
            # Sanity check + populate the component list from the instrument.
            self._populate_components()
        except Exception as exc:
            messagebox.showerror("Connection failed", f"Could not connect to {port}:\n{exc}")
            if self.analyzer is not None:
                self.analyzer.disconnect()
            self.analyzer = None
            return

        self.connect_btn.config(text="Disconnect")
        self.start_btn.config(state="normal")
        self.component_combo.config(state="readonly")
        self.info_btn.config(state="normal")
        self.status_var.set(f"Connected to {port} (AK-ID={ak_id!r}).")

        # Live polling (current value + graph) starts as soon as we're
        # connected — logging to CSV remains a separate, optional step.
        self._start_polling()

    def _disconnect(self):
        self._stop_polling()
        self._stop_logging()
        if self.analyzer is not None:
            self.analyzer.disconnect()
        self.analyzer = None
        self.connect_btn.config(text="Connect")
        self.start_btn.config(state="disabled")
        self.component_combo.config(state="disabled")
        self.info_btn.config(state="disabled")
        self.status_var.set("Disconnected.")

    def _populate_components(self):
        """Query AKMP to discover the instrument's measuring components and
        show them by name in the dropdown. This doubles as the connection
        sanity check (like the SO2 logger's test read): AKMP is an 'A'
        command, always available, so if the AK-ID is wrong or the wiring
        is bad the instrument answers "????" and read_measuring_components
        raises — deliberately NOT caught here, so it propagates up to
        _connect()'s except block and aborts the connection with a clear
        error instead of silently falling back to generic numbers.
        Falls back to plain numbers 1-5 only if AKMP succeeds but returns
        nothing parseable (a legitimately empty/unusual reply, not a
        communication failure)."""
        comps = self.analyzer.read_measuring_components(0)

        if comps:
            values = [f"{num}: {c.name} (0-{c.range_max})" for num, c in sorted(comps.items())]
        else:
            values = [str(n) for n in range(1, 6)]

        self.component_combo["values"] = values
        self.component_combo.set(values[0])
        self._on_component_change()

    def _on_component_change(self, event=None):
        selection = self.component_var.get()
        # selection is either "N" or "N: NAME (0-RANGE)" — component number
        # is always the leading digits before ':' or the whole string.
        number_part = selection.split(":")[0].strip()
        try:
            self._current_component = int(number_part)
        except ValueError:
            self._current_component = 1

    def _show_instrument_info(self):
        if self.analyzer is None or not self.analyzer.is_connected:
            return
        lines = []
        try:
            lines.append(f"Serial number: {self.analyzer.read_serial_number()}")
        except SidorProtocolError as exc:
            lines.append(f"Serial number: (unavailable — {exc})")
        try:
            lines.append(f"Instrument identification: {self.analyzer.read_instrument_identification()}")
        except SidorProtocolError as exc:
            lines.append(f"Instrument identification: (unavailable — {exc})")
        try:
            lines.append(f"Status bits (AFLT): {self.analyzer.read_instrument_status()}")
        except SidorProtocolError as exc:
            lines.append(f"Status bits: (unavailable — {exc})")
        messagebox.showinfo("Instrument info", "\n\n".join(lines))

    # ------------------------------------------------------------------
    # CSV file selection
    # ------------------------------------------------------------------
    def _choose_csv_file(self):
        path = filedialog.asksaveasfilename(
            title="Choose CSV log file",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.csv_path = path
            self.csv_path_var.set(path)

    # ------------------------------------------------------------------
    # Live polling — starts automatically once connected, independent of
    # whether CSV logging is turned on. Feeds both the current-value
    # display and the historical plot.
    # ------------------------------------------------------------------
    def _start_polling(self):
        self.stop_event.clear()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()
        self.polling = True

    def _stop_polling(self):
        self.stop_event.set()
        if self.poll_thread is not None:
            self.poll_thread.join(timeout=2)
        self.poll_thread = None
        self.polling = False

    def _poll_loop(self):
        """Runs in a background thread; never touches Tk widgets directly."""
        while not self.stop_event.is_set():
            start = time.monotonic()
            component = self._current_component
            try:
                values = self.analyzer.read_measuring_values(component)
                value = values.get(component)
                if value is None:
                    self.data_queue.put(("error", f"Component {component}: no numeric value (raw placeholder)."))
                else:
                    ts = datetime.now()
                    self.data_queue.put(("reading", ts, component, value))
            except SidorNoMeasuringValue:
                self.data_queue.put(("error", f"Component {component}: no measuring value available."))
            except Exception as exc:
                self.data_queue.put(("error", str(exc)))

            interval = self._current_interval
            elapsed = time.monotonic() - start
            remaining = max(0.0, interval - elapsed)
            self.stop_event.wait(remaining)

    # ------------------------------------------------------------------
    # CSV logging — a separate on/off toggle layered on top of the live
    # polling loop above; when on, every polled reading is also appended
    # to the chosen CSV file.
    # ------------------------------------------------------------------
    def _toggle_logging(self):
        if self.logging:
            self._stop_logging()
        else:
            self._start_logging()

    def _start_logging(self):
        if self.analyzer is None or not self.analyzer.is_connected:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        if not self.csv_path:
            messagebox.showwarning("No CSV file", "Choose a CSV file to log to first.")
            return

        self._ensure_csv_header()
        self.logging = True
        self.start_btn.config(text="Stop logging")
        self.status_var.set(f"Logging to {self.csv_path}")

    def _stop_logging(self):
        self.logging = False
        self.start_btn.config(text="Start logging")
        if self.analyzer is not None and self.analyzer.is_connected:
            self.status_var.set("Logging stopped (live view still active).")

    def _ensure_csv_header(self):
        try:
            with open(self.csv_path, "r", newline="") as f:
                existing = f.read(1)
        except FileNotFoundError:
            existing = ""
        if not existing:
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "component", "value"])

    def _drain_queue(self):
        """Runs on the Tk main thread; safe to touch widgets here."""
        try:
            while True:
                item = self.data_queue.get_nowait()
                if item[0] == "reading":
                    _, ts, component, value = item
                    self._handle_reading(ts, component, value)
                elif item[0] == "error":
                    _, message = item
                    self.status_var.set(f"Error: {message}")
        except queue.Empty:
            pass
        finally:
            self.after(200, self._drain_queue)

    def _handle_reading(self, ts: datetime, component: int, value: float):
        self.timestamps.append(ts)
        self.values.append(value)

        self.value_var.set(f"{value:g}")
        self.timestamp_var.set(ts.strftime("Last reading: %Y-%m-%d %H:%M:%S") + f"  (component {component})")

        if self.logging:
            self._append_csv_row(ts, component, value)
        self._update_plot()

    def _append_csv_row(self, ts: datetime, component: int, value: float):
        with open(self.csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([ts.isoformat(), component, value])

    def _update_plot(self):
        window_seconds = self.PLOT_WINDOWS.get(self.plot_window_var.get())
        if window_seconds is None or not self.timestamps:
            plot_ts, plot_vals = self.timestamps, self.values
        else:
            cutoff = datetime.now() - timedelta(seconds=window_seconds)
            plot_ts, plot_vals = [], []
            for t, v in zip(self.timestamps, self.values):
                if t >= cutoff:
                    plot_ts.append(t)
                    plot_vals.append(v)

        self.line.set_data(plot_ts, plot_vals)
        self.ax.relim()
        self.ax.autoscale_view()
        self.fig.autofmt_xdate()
        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    def _on_close(self):
        self._stop_polling()
        if self.analyzer is not None:
            self.analyzer.disconnect()
        self.destroy()


def main():
    app = SidorLoggerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
