import csv
import re
import sys
import time
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    messagebox.showerror("Thiếu thư viện", "Bạn cần cài pyserial:\npip install pyserial")
    sys.exit(1)

try:
    import pywt
    HAS_PYWT = True
except ImportError:
    HAS_PYWT = False

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk


# =========================================================
# CONFIG
# =========================================================
BAUD_RATE = 115200

SENSOR_SAMPLE_RATE = 100
PPG_SAMPLE_RATE = 50
ECG_SAMPLE_RATE = SENSOR_SAMPLE_RATE
ECG_DEFAULT_RECORD_TIME = 60

PPG_DEFAULT_RECORD_TIME = 60
PPG_WARMUP_TIME = 15

WINDOW_SIZE = 3
WAVELET_NAME = "db4"
WAVELET_LEVEL = 3

BASE_DIR = Path("data_csv")
ECG_DIR = BASE_DIR / "ECG"
PPG_DIR = BASE_DIR / "PPG"
ECG_DIR.mkdir(parents=True, exist_ok=True)
PPG_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================
# COLOR PALETTE  –  "Warm Ember"
# Inspired by analog medical instruments — warm charcoal,
# amber glow, terracotta signals, soft sage accent.
# =========================================================
C = {
    # Backgrounds — warm charcoal tones
    "bg_root":    "#1C1A17",   # near-black with warm undertone
    "bg_card":    "#252119",   # dark coffee
    "bg_panel":   "#2D2921",   # slightly lighter
    "bg_input":   "#352F24",   # warm dark tan
    "bg_listbox": "#1A1814",   # deepest background
    "bg_hover":   "#44392A",   # warm hover

    # Accent & highlight — amber / ember family
    "accent":     "#F0A500",   # warm amber — primary
    "accent2":    "#E8C56D",   # pale gold
    "accent3":    "#D95F3B",   # terracotta / burnt orange
    "accent4":    "#A8C97F",   # sage green — contrast accent
    "accent5":    "#C49A6C",   # warm tan / copper

    # Text
    "text_primary":   "#F5ECD7",   # warm white/cream
    "text_secondary": "#B8A88A",   # warm grey
    "text_dim":       "#6B5E4A",   # muted warm grey
    "text_green":     "#98C379",   # soft green
    "text_red":       "#E06C75",   # soft red

    # Borders / dividers
    "border":        "#3E3529",
    "border_bright": "#F0A500",
}


# =========================================================
# UTILS
# =========================================================
def safe_filename(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    return name.strip("_") or "unknown"


def moving_average_filter(data: np.ndarray, window_size: int) -> np.ndarray:
    if len(data) == 0:
        return data
    if window_size <= 1:
        return data.copy()
    return np.convolve(data, np.ones(window_size) / window_size, mode="same")


def wavelet_denoise(data: np.ndarray, wavelet_name: str = "db4", level: int = 3) -> np.ndarray:
    if not HAS_PYWT:
        return np.full_like(data, np.nan, dtype=float)
    if len(data) < 8:
        return data.copy()
    wavelet = pywt.Wavelet(wavelet_name)
    max_level = pywt.dwt_max_level(data_len=len(data), filter_len=wavelet.dec_len)
    level = min(level, max_level)
    if level < 1:
        return data.copy()
    coeffs = pywt.wavedec(data, wavelet_name, mode="symmetric", level=level)
    sigma = np.median(np.abs(coeffs[-1])) / 0.6745 if len(coeffs[-1]) > 0 else 0
    threshold = sigma * np.sqrt(2 * np.log(len(data)))
    new_coeffs = [coeffs[0]]
    for detail in coeffs[1:]:
        new_coeffs.append(pywt.threshold(detail, value=threshold, mode="soft"))
    reconstructed = pywt.waverec(new_coeffs, wavelet_name, mode="symmetric")
    return reconstructed[:len(data)]


def estimate_bpm_from_ir(ir_values: np.ndarray, sample_rate: float = SENSOR_SAMPLE_RATE):
    if len(ir_values) < int(sample_rate * 3):
        return np.full(len(ir_values), np.nan), np.full(len(ir_values), np.nan)

    signal = ir_values.astype(float)
    signal = signal - moving_average_filter(signal, max(3, int(sample_rate)))
    threshold = np.nanmean(signal) + 0.6 * np.nanstd(signal)
    min_distance = max(1, int(sample_rate * 0.35))
    peaks = []
    last_peak = -min_distance

    for i in range(1, len(signal) - 1):
        if (
            signal[i] > threshold
            and signal[i] > signal[i - 1]
            and signal[i] >= signal[i + 1]
            and i - last_peak >= min_distance
        ):
            peaks.append(i)
            last_peak = i

    bpm = np.full(len(ir_values), np.nan)
    avg_bpm = np.full(len(ir_values), np.nan)
    beat_bpms = []

    for prev, cur in zip(peaks, peaks[1:]):
        interval_s = (cur - prev) / sample_rate
        if interval_s <= 0:
            continue
        value = 60.0 / interval_s
        if 35 <= value <= 220:
            bpm[cur:] = value
            beat_bpms.append(value)
            avg_bpm[cur:] = float(np.mean(beat_bpms[-8:]))

    return bpm, avg_bpm


def parse_sensor_line(line: str):
    line = line.strip()
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    try:
        label = parts[0].upper()
        if label in ("EEG", "ECG") and len(parts) >= 2:
            return np.nan, np.nan, float(parts[1])
        if label == "PPG" and len(parts) >= 3:
            return float(parts[1]), float(parts[2]), np.nan
        if label in ("BOTH", "DATA", "SENSOR") and len(parts) >= 4:
            return float(parts[1]), float(parts[2]), float(parts[3])
        if parts[0].upper() in ("DATA", "SENSOR") and len(parts) >= 4:
            return float(parts[1]), float(parts[2]), float(parts[3])
        if len(parts) >= 3:
            return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
    return None


def parse_ecg_line(line: str):
    parsed = parse_sensor_line(line)
    if parsed is not None:
        _, _, ecg = parsed
        return ecg, np.nan
    line = line.strip()
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    try:
        if parts[0].upper() == "ECG" and len(parts) >= 3:
            return float(parts[1]), float(parts[2])
        if len(parts) >= 2:
            return float(parts[0]), float(parts[1])
        if len(parts) == 1:
            v = float(parts[0])
            return v, np.nan
    except ValueError:
        return None
    return None


def parse_ppg_line(line: str):
    parsed = parse_sensor_line(line)
    if parsed is not None:
        red, ir, _ = parsed
        return red, ir, np.nan, np.nan
    line = line.strip()
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    try:
        if parts[0].upper() == "PPG" and len(parts) >= 4:
            return np.nan, int(float(parts[1])), float(parts[2]), float(parts[3])
        if len(parts) >= 3:
            return np.nan, int(float(parts[0])), float(parts[1]), float(parts[2])
        if len(parts) == 1:
            return np.nan, int(float(parts[0])), np.nan, np.nan
    except ValueError:
        return None
    return None


def read_csv_dicts(csv_path: Path):
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float_array(rows, key):
    values = []
    for r in rows:
        text = r.get(key, "")
        try:
            values.append(float(text))
        except (ValueError, TypeError):
            values.append(np.nan)
    return np.array(values, dtype=float)


def csv_duration_s(csv_path: Path) -> float:
    try:
        rows = read_csv_dicts(csv_path)
        if not rows:
            return 1.0
        t = to_float_array(rows, "time_s")
        if len(t) == 0 or np.isnan(t).all():
            return 1.0
        return max(1.0, float(np.nanmax(t)))
    except Exception:
        return 1.0


# =========================================================
# ZOOM MANAGER — scroll wheel + Ctrl+scroll zoom on canvas
# =========================================================
class PlotZoomManager:
    """
    Handles interactive zoom on a Matplotlib axes via mouse scroll wheel.
    - Scroll up  → zoom in  (centered on cursor)
    - Scroll down → zoom out
    - Ctrl+scroll → zoom Y axis only
    - Middle-click drag → pan
    - Double-click → reset to auto view
    """

    ZOOM_FACTOR = 1.25   # zoom speed per scroll step

    def __init__(self, canvas, ax, figure, get_axes_fn=None):
        self.canvas = canvas
        self.ax = ax
        self.figure = figure
        self.get_axes_fn = get_axes_fn  # returns list of all current axes if twin axes exist

        self._pan_start = None
        self._pan_xlim = None
        self._pan_ylim = None

        canvas.mpl_connect("scroll_event",        self._on_scroll)
        canvas.mpl_connect("button_press_event",  self._on_press)
        canvas.mpl_connect("button_release_event",self._on_release)
        canvas.mpl_connect("motion_notify_event", self._on_motion)
        canvas.mpl_connect("axes_enter_event",    self._on_axes_enter)

        # Tooltip hint
        self._hint_visible = True

    def _all_axes(self):
        if self.get_axes_fn:
            return self.get_axes_fn()
        return [self.ax]

    def _on_scroll(self, event):
        if event.inaxes is None:
            return
        ax = event.inaxes
        ctrl = (event.key == "control") or (event.key == "ctrl")

        factor = self.ZOOM_FACTOR if event.button == "up" else 1.0 / self.ZOOM_FACTOR

        xdata, ydata = event.xdata, event.ydata
        if xdata is None or ydata is None:
            return

        if not ctrl:
            # Zoom X axis
            xlim = ax.get_xlim()
            new_xlim = [
                xdata - (xdata - xlim[0]) / factor,
                xdata + (xlim[1] - xdata) / factor,
            ]
            # Apply same X to all axes (twin axes share X)
            for a in self._all_axes():
                a.set_xlim(new_xlim)

        # Zoom Y axis always
        ylim = ax.get_ylim()
        new_ylim = [
            ydata - (ydata - ylim[0]) / factor,
            ydata + (ylim[1] - ydata) / factor,
        ]
        ax.set_ylim(new_ylim)

        self.canvas.draw_idle()

    def _on_press(self, event):
        if event.button == 2 and event.inaxes:   # middle click → pan
            self._pan_start = (event.xdata, event.ydata)
            self._pan_ax    = event.inaxes
            self._pan_xlim  = list(event.inaxes.get_xlim())
            self._pan_ylim  = list(event.inaxes.get_ylim())
        elif event.dblclick and event.inaxes:     # double-click → reset
            self._reset_view(event.inaxes)

    def _on_release(self, event):
        if event.button == 2:
            self._pan_start = None

    def _on_motion(self, event):
        if self._pan_start is None or event.inaxes is None:
            return
        if event.xdata is None or event.ydata is None:
            return
        dx = self._pan_start[0] - event.xdata
        dy = self._pan_start[1] - event.ydata
        ax = self._pan_ax
        ax.set_xlim(self._pan_xlim[0] + dx, self._pan_xlim[1] + dx)
        ax.set_ylim(self._pan_ylim[0] + dy, self._pan_ylim[1] + dy)
        self.canvas.draw_idle()

    def _on_axes_enter(self, event):
        pass  # could show hint tooltip here

    def _reset_view(self, ax):
        ax.autoscale()
        self.canvas.draw_idle()


# =========================================================
# GUI APP
# =========================================================
class HeartSensorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Heart Sensor UI — Nhóm 2")
        self.root.geometry("1280x800")
        self.root.minsize(1050, 680)

        self.serial_obj = None
        self.measure_thread = None
        self.is_measuring = False

        self.current_signal_type = "ECG"
        self.current_ppg_view = "IR"
        self.current_files = []
        self.current_file_index = 0

        self.realtime_mode = None
        self.live_t = deque(maxlen=3000)
        self.live_y1 = deque(maxlen=3000)
        self.live_y2 = deque(maxlen=3000)
        self.live_y3 = deque(maxlen=3000)
        self.live_plot_running = False
        self.realtime_xmax = 10

        self.show_ecg_raw = tk.BooleanVar(value=True)
        self.show_ecg_filtered = tk.BooleanVar(value=True)
        self.show_ecg_ma = tk.BooleanVar(value=True)
        self.show_ecg_wavelet = tk.BooleanVar(value=True)

        self.show_ppg_ir = tk.BooleanVar(value=True)
        self.show_ppg_bpm = tk.BooleanVar(value=True)
        self.show_ppg_avg_bpm = tk.BooleanVar(value=True)
        self.show_ppg_wavelet = tk.BooleanVar(value=True)

        self.zoom_manager = None  # set after canvas is created

        self.setup_style()
        self.build_ui()
        self.refresh_ports()
        self.refresh_file_list()

        self.root.bind("<Left>",  lambda e: self.prev_file())
        self.root.bind("<Right>", lambda e: self.next_file())
        self.root.bind("<Up>",    lambda e: self.prev_file())
        self.root.bind("<Down>",  lambda e: self.next_file())

    # ── STYLE ────────────────────────────────────────────
    def setup_style(self):
        self.root.configure(bg=C["bg_root"])
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("TFrame",        background=C["bg_root"])
        style.configure("Card.TFrame",   background=C["bg_card"])
        style.configure("Panel.TFrame",  background=C["bg_panel"])

        style.configure("TLabel",
            background=C["bg_root"], foreground=C["text_primary"],
            font=("Consolas", 10))
        style.configure("Title.TLabel",
            background=C["bg_root"], foreground=C["accent"],
            font=("Consolas", 22, "bold"))
        style.configure("Sub.TLabel",
            background=C["bg_root"], foreground=C["text_secondary"],
            font=("Consolas", 9))
        style.configure("Team.TLabel",
            background=C["bg_root"], foreground=C["accent5"],
            font=("Consolas", 9, "italic"))
        style.configure("Card.TLabel",
            background=C["bg_card"], foreground=C["text_primary"],
            font=("Consolas", 10))
        style.configure("CardTitle.TLabel",
            background=C["bg_card"], foreground=C["accent"],
            font=("Consolas", 13, "bold"))
        style.configure("Status.TLabel",
            background=C["bg_panel"], foreground=C["text_green"],
            font=("Consolas", 9))
        style.configure("SectionHead.TLabel",
            background=C["bg_card"], foreground=C["accent5"],
            font=("Consolas", 9, "bold"))

        for name, bg, fg, hover in [
            ("TButton",        C["bg_input"],  C["text_primary"],  C["bg_hover"]),
            ("Accent.TButton", C["accent"],    C["bg_root"],       "#FFCA28"),
            ("Accent2.TButton",C["accent4"],   C["bg_root"],       "#C5E1A5"),
            ("Danger.TButton", C["accent3"],   "#FFFFFF",          "#EF8A6F"),
            ("Warn.TButton",   C["accent2"],   C["bg_root"],       "#F5D98A"),
        ]:
            style.configure(name,
                background=bg, foreground=fg,
                font=("Consolas", 9, "bold"),
                padding=7, relief="flat", borderwidth=0)
            style.map(name, background=[("active", hover)])

        style.configure("TCombobox",
            fieldbackground=C["bg_input"], background=C["bg_input"],
            foreground=C["text_primary"], arrowcolor=C["accent"],
            selectbackground=C["bg_hover"])

        style.configure("TEntry",
            fieldbackground=C["bg_input"], foreground=C["text_primary"],
            insertcolor=C["accent"])

        style.configure("TCheckbutton",
            background=C["bg_card"], foreground=C["text_secondary"],
            font=("Consolas", 9))
        style.map("TCheckbutton",
            background=[("active", C["bg_card"])],
            foreground=[("active", C["accent"])])

        style.configure("TRadiobutton",
            background=C["bg_card"], foreground=C["text_secondary"],
            font=("Consolas", 9))
        style.map("TRadiobutton",
            background=[("active", C["bg_card"])],
            foreground=[("active", C["accent"])])

        style.configure("Horizontal.TProgressbar",
            background=C["accent"], troughcolor=C["bg_input"],
            bordercolor=C["border"], lightcolor=C["accent"],
            darkcolor=C["accent"])

        style.configure("TSeparator", background=C["border"])

    # ── BUILD UI ─────────────────────────────────────────
    def build_ui(self):
        # ── HEADER ──
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=20, pady=(14, 6))

        left_h = ttk.Frame(header)
        left_h.pack(side="left")

        ttk.Label(left_h, text="♥  HEART SENSOR UI", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left_h,
            text="ECG / PPG Real-time Logger  ·  CSV Storage  ·  Interactive Viewer",
            style="Sub.TLabel").pack(anchor="w", pady=(1, 0))

        team_frame = tk.Frame(header, bg=C["bg_card"],
                              highlightbackground=C["accent5"],
                              highlightthickness=1)
        team_frame.pack(side="right", padx=(0, 4), pady=2, ipadx=14, ipady=8)

        tk.Label(team_frame, text="NHÓM 2",
                 bg=C["bg_card"], fg=C["accent"],
                 font=("Consolas", 11, "bold")).pack(anchor="center")

        members = ["Lưu Duy Trường", "Nguyễn Chí Tâm", "Nguyễn Tiến Thịnh"]
        for m in members:
            tk.Label(team_frame, text=f"  ▸  {m}",
                     bg=C["bg_card"], fg=C["text_secondary"],
                     font=("Consolas", 9)).pack(anchor="w")

        div = tk.Frame(self.root, bg=C["accent"], height=1)
        div.pack(fill="x", padx=20, pady=(4, 10))

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        left = tk.Frame(body, bg=C["bg_card"],
                        highlightbackground=C["border"],
                        highlightthickness=1)
        left.pack(side="left", fill="y", padx=(0, 10), ipadx=6, ipady=6)

        right = tk.Frame(body, bg=C["bg_card"],
                         highlightbackground=C["border"],
                         highlightthickness=1)
        right.pack(side="right", fill="both", expand=True, ipadx=6, ipady=6)

        self.build_control_panel(left)
        self.build_plot_panel(right)

    # ── CONTROL PANEL ────────────────────────────────────
    def build_control_panel(self, parent):
        def section(text):
            f = tk.Frame(parent, bg=C["bg_card"])
            f.pack(fill="x", padx=12, pady=(14, 2))
            tk.Label(f, text=f"  {text}",
                     bg=C["accent5"], fg=C["bg_root"],
                     font=("Consolas", 8, "bold"),
                     padx=6, pady=2).pack(anchor="w")
            return f

        def lbl(p, text):
            tk.Label(p, text=text,
                     bg=C["bg_card"], fg=C["text_secondary"],
                     font=("Consolas", 9)).pack(anchor="w", pady=(6, 1))

        def entry(p, var, w=28):
            e = tk.Entry(p, textvariable=var, width=w,
                         bg=C["bg_input"], fg=C["text_primary"],
                         insertbackground=C["accent"],
                         relief="flat", font=("Consolas", 10),
                         highlightbackground=C["border"],
                         highlightthickness=1)
            e.pack(fill="x", pady=(0, 2))
            return e

        tk.Label(parent, text="CONTROL PANEL",
                 bg=C["bg_card"], fg=C["accent"],
                 font=("Consolas", 13, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4))

        section("SERIAL PORT")
        port_frame = tk.Frame(parent, bg=C["bg_card"])
        port_frame.pack(fill="x", padx=14, pady=4)

        self.port_combo = ttk.Combobox(port_frame, width=28, state="readonly")
        self.port_combo.pack(fill="x", pady=(4, 6))

        row = tk.Frame(port_frame, bg=C["bg_card"])
        row.pack(fill="x")
        self._btn(row, "↺ Refresh COM", self.refresh_ports, "TButton").pack(
            side="left", expand=True, fill="x")
        self._btn(row, "⚡ Test", self.test_serial, "TButton").pack(
            side="left", expand=True, fill="x", padx=(6, 0))

        section("THÔNG TIN")
        info = tk.Frame(parent, bg=C["bg_card"])
        info.pack(fill="x", padx=14, pady=4)

        lbl(info, "Tên người đo")
        self.person_var = tk.StringVar(value="unknown")
        entry(info, self.person_var)

        lbl(info, "Thời gian đo EEG (s)")
        self.ecg_time_var = tk.StringVar(value=str(ECG_DEFAULT_RECORD_TIME))
        entry(info, self.ecg_time_var)

        lbl(info, "Thời gian lưu PPG sau warm-up (s)")
        self.ppg_time_var = tk.StringVar(value=str(PPG_DEFAULT_RECORD_TIME))
        entry(info, self.ppg_time_var)

        self.plot_after_var = tk.BooleanVar(value=True)
        chk = tk.Checkbutton(info,
            text=" Plot realtime & giữ sau khi đo",
            variable=self.plot_after_var,
            bg=C["bg_card"], fg=C["text_secondary"],
            selectcolor=C["bg_input"],
            activebackground=C["bg_card"],
            activeforeground=C["accent"],
            font=("Consolas", 9))
        chk.pack(anchor="w", pady=(6, 2))

        section("ĐO ĐẠC")
        mf = tk.Frame(parent, bg=C["bg_card"])
        mf.pack(fill="x", padx=14, pady=6)

        self._btn(mf, "▶  ĐO EEG",   self.start_measure_ecg,  "Accent.TButton").pack(fill="x", pady=3)
        self._btn(mf, "▶  ĐO PPG",   self.start_measure_ppg,  "Accent2.TButton").pack(fill="x", pady=3)
        self._btn(mf, "▶  ĐO CẢ 2",  self.start_measure_both, "TButton").pack(fill="x", pady=3)
        self._btn(mf, "■  DỪNG ĐO",  self.stop_measurement,   "Danger.TButton").pack(fill="x", pady=3)

        self.progress = ttk.Progressbar(parent, orient="horizontal",
                                        mode="determinate", length=260)
        self.progress.pack(fill="x", padx=14, pady=(12, 4))

        status_bg = tk.Frame(parent, bg=C["bg_panel"],
                             highlightbackground=C["border"],
                             highlightthickness=1)
        status_bg.pack(fill="x", padx=14, pady=(4, 14))

        self.status_var = tk.StringVar(
            value="[READY]  Chọn COM, nhập tên, rồi bắt đầu đo.")
        tk.Label(status_bg,
                 textvariable=self.status_var,
                 bg=C["bg_panel"], fg=C["text_green"],
                 font=("Consolas", 9),
                 wraplength=300, justify="left",
                 padx=8, pady=6).pack(fill="x")

    def _btn(self, parent, text, cmd, style="TButton"):
        return ttk.Button(parent, text=text, command=cmd, style=style)

    def style_toolbar(self):
        if not hasattr(self, "toolbar"):
            return
        self.toolbar.configure(bg=C["bg_card"])
        for child in self.toolbar.winfo_children():
            try:
                child.configure(bg=C["bg_card"])
            except tk.TclError:
                pass
            if isinstance(child, tk.Button):
                child.configure(
                    bg=C["bg_input"], fg=C["text_primary"],
                    activebackground=C["bg_hover"],
                    activeforeground=C["text_primary"],
                    relief="flat", borderwidth=0,
                    highlightthickness=0)
            elif isinstance(child, tk.Label):
                child.configure(bg=C["bg_card"], fg=C["text_secondary"])

    # ── PLOT PANEL ───────────────────────────────────────
    def build_plot_panel(self, parent):
        top = tk.Frame(parent, bg=C["bg_card"])
        top.pack(fill="x", padx=14, pady=(14, 4))

        tk.Label(top, text="PLOT VIEWER",
                 bg=C["bg_card"], fg=C["accent"],
                 font=("Consolas", 13, "bold")).pack(side="left")

        self._btn(top, "Next ⟶", self.next_file, "TButton").pack(side="right", padx=(6, 0))
        self._btn(top, "⟵ Prev", self.prev_file, "TButton").pack(side="right")

        opts = tk.Frame(parent, bg=C["bg_card"])
        opts.pack(fill="x", padx=14, pady=4)

        tk.Label(opts, text="Loại:",
                 bg=C["bg_card"], fg=C["text_secondary"],
                 font=("Consolas", 9)).pack(side="left")

        self.signal_var = tk.StringVar(value="ECG")
        for val in ("ECG", "PPG"):
            rb = tk.Radiobutton(opts, text=val, value=val,
                variable=self.signal_var,
                command=self.on_signal_change,
                bg=C["bg_card"], fg=C["text_primary"],
                selectcolor=C["bg_input"],
                activebackground=C["bg_card"],
                activeforeground=C["accent"],
                font=("Consolas", 9))
            rb.pack(side="left", padx=6)

        tk.Label(opts, text="   PPG view:",
                 bg=C["bg_card"], fg=C["text_secondary"],
                 font=("Consolas", 9)).pack(side="left", padx=(12, 0))

        self.ppg_view_var = tk.StringVar(value="IR")
        for val in ("IR", "BPM"):
            rb = tk.Radiobutton(opts, text=val, value=val,
                variable=self.ppg_view_var,
                command=self.replot_current,
                bg=C["bg_card"], fg=C["text_primary"],
                selectcolor=C["bg_input"],
                activebackground=C["bg_card"],
                activeforeground=C["accent4"],
                font=("Consolas", 9))
            rb.pack(side="left", padx=4)

        self._btn(opts, "↺ Refresh", self.refresh_file_list, "TButton").pack(side="right")

        self.visibility_frame = tk.Frame(parent, bg=C["bg_card"])
        self.visibility_frame.pack(fill="x", padx=14, pady=(4, 4))
        self.build_visibility_controls()

        search_frame = tk.Frame(parent, bg=C["bg_card"])
        search_frame.pack(fill="x", padx=14, pady=(6, 4))

        tk.Label(search_frame, text="🔍 Tìm theo tên:",
                 bg=C["bg_card"], fg=C["text_secondary"],
                 font=("Consolas", 9)).pack(anchor="w")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_file_list())
        se = tk.Entry(search_frame, textvariable=self.search_var,
                      bg=C["bg_input"], fg=C["text_primary"],
                      insertbackground=C["accent"],
                      relief="flat", font=("Consolas", 10),
                      highlightbackground=C["border"],
                      highlightthickness=1)
        se.pack(fill="x", pady=(4, 0))

        file_area = tk.Frame(parent, bg=C["bg_card"])
        file_area.pack(fill="x", padx=14, pady=(6, 4))

        lb_frame = tk.Frame(file_area, bg=C["bg_card"])
        lb_frame.pack(fill="x")

        scrollbar = tk.Scrollbar(lb_frame, orient="vertical",
                                 bg=C["bg_input"],
                                 troughcolor=C["bg_root"],
                                 activebackground=C["accent"])
        scrollbar.pack(side="right", fill="y")

        self.file_listbox = tk.Listbox(
            lb_frame, height=6,
            bg=C["bg_listbox"], fg=C["text_primary"],
            selectbackground=C["accent"], selectforeground=C["bg_root"],
            font=("Consolas", 10),
            activestyle="none",
            highlightbackground=C["border"],
            highlightthickness=1,
            relief="flat",
            yscrollcommand=scrollbar.set)
        self.file_listbox.pack(fill="x")
        scrollbar.config(command=self.file_listbox.yview)
        self.file_listbox.bind("<<ListboxSelect>>", self.on_file_selected)

        del_row = tk.Frame(file_area, bg=C["bg_card"])
        del_row.pack(fill="x", pady=(6, 0))

        self._btn(del_row, "🗑  Xóa file đang chọn",
                  self.delete_selected_file, "Danger.TButton").pack(
            side="left", expand=True, fill="x")
        self._btn(del_row, "🗑  Xóa toàn bộ tập data",
                  self.delete_all_files, "Warn.TButton").pack(
            side="left", expand=True, fill="x", padx=(6, 0))

        # ── Plot canvas ──
        plot_container = tk.Frame(parent, bg=C["bg_card"])
        plot_container.pack(fill="both", expand=True, padx=14, pady=(8, 2))

        self.figure = Figure(figsize=(8, 5), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.figure.patch.set_facecolor(C["bg_card"])
        self._style_axis_base()

        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_container)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        toolbar_frame = tk.Frame(plot_container, bg=C["bg_card"])
        toolbar_frame.pack(fill="x", pady=(4, 0))
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side="left")
        self.style_toolbar()

        # ── Zoom manager ──
        self.zoom_manager = PlotZoomManager(
            self.canvas, self.ax, self.figure,
            get_axes_fn=lambda: list(self.figure.axes))

        # ── Zoom hint bar ──
        hint_bar = tk.Frame(parent, bg=C["bg_panel"],
                            highlightbackground=C["border"],
                            highlightthickness=1)
        hint_bar.pack(fill="x", padx=14, pady=(2, 8))
        tk.Label(hint_bar,
                 text="  🔍  Lăn chuột: zoom X+Y  ·  Ctrl + lăn: zoom Y  "
                      "·  Giữ nút giữa: pan  ·  Double-click: reset  "
                      "·  ← / → : đổi file",
                 bg=C["bg_panel"], fg=C["text_dim"],
                 font=("Consolas", 8),
                 padx=4, pady=3).pack(anchor="w")

    # ── SIGNAL VISIBILITY CONTROLS ───────────────────────
    def _plot_check(self, parent, text, variable):
        cb = tk.Checkbutton(
            parent, text=text, variable=variable,
            command=self.replot_current,
            bg=C["bg_card"], fg=C["text_primary"],
            selectcolor=C["bg_input"],
            activebackground=C["bg_card"],
            activeforeground=C["accent"],
            font=("Consolas", 9))
        cb.pack(side="left", padx=(0, 10))
        return cb

    def build_visibility_controls(self):
        for child in self.visibility_frame.winfo_children():
            child.destroy()

        tk.Label(self.visibility_frame, text="Hiển thị:",
                 bg=C["bg_card"], fg=C["text_secondary"],
                 font=("Consolas", 9)).pack(side="left", padx=(0, 10))

        self._plot_check(self.visibility_frame, "Raw ECG",    self.show_ecg_raw)
        self._plot_check(self.visibility_frame, "Filtered",   self.show_ecg_filtered)
        self._plot_check(self.visibility_frame, "Moving Avg", self.show_ecg_ma)
        self._plot_check(self.visibility_frame, "Wavelet",    self.show_ecg_wavelet)

        tk.Label(self.visibility_frame, text=" | ",
                 bg=C["bg_card"], fg=C["text_dim"],
                 font=("Consolas", 9)).pack(side="left")

        self._plot_check(self.visibility_frame, "IR",      self.show_ppg_ir)
        self._plot_check(self.visibility_frame, "BPM",     self.show_ppg_bpm)
        self._plot_check(self.visibility_frame, "Avg BPM", self.show_ppg_avg_bpm)
        self._plot_check(self.visibility_frame, "Wavelet", self.show_ppg_wavelet)

    # ── DELETE FILES ─────────────────────────────────────
    def delete_selected_file(self):
        if not self.current_files:
            messagebox.showinfo("Thông báo", "Không có file nào để xóa.")
            return
        csv_file = self.current_files[self.current_file_index]
        confirm = messagebox.askyesno(
            "Xác nhận xóa",
            f"Bạn có chắc muốn xóa file:\n\n  {csv_file.name}\n\nHành động này không thể hoàn tác!",
            icon="warning")
        if not confirm:
            return
        try:
            csv_file.unlink()
            self.set_status(f"[DELETE] Đã xóa: {csv_file.name}")
            self.refresh_file_list()
            self.replot_current()
        except Exception as e:
            messagebox.showerror("Lỗi xóa file", str(e))

    def delete_all_files(self):
        if not self.current_files:
            messagebox.showinfo("Thông báo", "Không có file nào để xóa.")
            return
        signal = self.signal_var.get()
        folder = ECG_DIR if signal == "ECG" else PPG_DIR
        count = len(self.current_files)
        confirm = messagebox.askyesno(
            "⚠ XÓA TOÀN BỘ",
            f"Bạn có chắc muốn xóa TẤT CẢ {count} file {signal}\n"
            f"trong thư mục:\n  {folder}\n\nHành động này KHÔNG THỂ hoàn tác!",
            icon="warning")
        if not confirm:
            return
        confirm2 = messagebox.askyesno(
            "Xác nhận lần 2",
            f"Xóa {count} file {signal}?\nNhấn YES để tiếp tục.",
            icon="warning")
        if not confirm2:
            return
        deleted = errors = 0
        for f in self.current_files:
            try:
                f.unlink(); deleted += 1
            except Exception:
                errors += 1
        self.set_status(f"[DELETE] Đã xóa {deleted}/{count} file {signal}.")
        if errors:
            messagebox.showwarning("Cảnh báo", f"Không xóa được {errors} file.")
        self.refresh_file_list()
        self.clear_plot("Đã xóa toàn bộ tập data.")

    # ── SERIAL ───────────────────────────────────────────
    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        values = [f"{p.device} - {p.description}" for p in ports]
        self.port_combo["values"] = values
        if values:
            self.port_combo.current(0)
            self.set_status(f"[COM] Tìm thấy {len(values)} cổng.")
        else:
            self.port_combo.set("")
            self.set_status("[WARN] Không tìm thấy cổng COM nào.")

    def get_selected_port(self):
        value = self.port_combo.get()
        if not value:
            return None
        return value.split(" - ")[0].strip()

    def open_serial(self):
        port = self.get_selected_port()
        if not port:
            raise RuntimeError("Chưa chọn cổng COM.")
        ser = serial.Serial(port, BAUD_RATE, timeout=1)
        time.sleep(1.5)
        ser.reset_input_buffer()
        return ser

    def test_serial(self):
        try:
            ser = self.open_serial()
            ser.close()
            messagebox.showinfo("Serial OK", "Mở cổng COM thành công.")
            self.set_status("[SERIAL] Kết nối OK.")
        except Exception as e:
            messagebox.showerror("Serial Error", str(e))
            self.set_status("[ERROR] Không mở được cổng COM.")

    def send_mode(self, ser, mode):
        ser.write((mode.upper() + "\n").encode())
        time.sleep(0.2)

    # ── REALTIME PLOT ─────────────────────────────────────
    def reset_realtime_plot(self, mode):
        self.realtime_mode = mode
        self.live_t.clear()
        self.live_y1.clear()
        self.live_y2.clear()
        self.live_y3.clear()
        self.live_plot_running = True
        self.ax.clear()
        self._style_axis_base()
        title = "Realtime ECG" if mode == "ECG" else "Realtime PPG"
        self.ax.set_title(title, color=C["accent"])
        self.ax.set_xlabel("Thời gian (s)")
        self.ax.set_ylabel("Điện áp (V)" if mode == "ECG" else "IR Value / BPM")
        self.ax.set_xlim(0, max(1, self.realtime_xmax))
        self.ax.grid(True, linestyle="--", alpha=0.18, color=C["border_bright"])
        self.canvas.draw_idle()

    def append_realtime_ecg(self, t, raw_v, filtered_v):
        self.live_t.append(t)
        self.live_y1.append(raw_v)
        self.live_y2.append(filtered_v)

    def append_realtime_ppg(self, t, red, ir, bpm, avg_bpm):
        self.live_t.append(t)
        self.live_y1.append(ir)
        self.live_y2.append(bpm)
        self.live_y3.append(avg_bpm)

    def update_realtime_plot(self):
        if not self.live_plot_running:
            return
        if len(self.live_t) > 2:
            for extra_ax in list(self.figure.axes)[1:]:
                extra_ax.remove()
            self.ax.clear()
            self._style_axis_base()
            t = np.array(self.live_t, dtype=float)

            if self.realtime_mode == "ECG":
                raw = np.array(self.live_y1, dtype=float)
                filtered = np.array(self.live_y2, dtype=float)
                if self.show_ecg_raw.get():
                    self.ax.plot(t, raw, label="Raw ECG",
                                 color=C["accent5"], alpha=0.55, linewidth=1.0)
                if self.show_ecg_filtered.get() and not np.isnan(filtered).all():
                    self.ax.plot(t, filtered, label="Filtered ECG",
                                 color=C["accent"], linewidth=1.2)
                self.ax.set_title(f"Realtime ECG | samples={len(t)}", color=C["accent"])
                self.ax.set_xlabel("Thời gian (s)")
                self.ax.set_ylabel("Điện áp (V)")
                self.ax.set_xlim(0, max(1, self.realtime_xmax))
                self.apply_auto_y_axis(self.ax, [raw, filtered])

            elif self.realtime_mode == "PPG":
                ir = np.array(self.live_y1, dtype=float)
                bpm = np.array(self.live_y2, dtype=float)
                avg = np.array(self.live_y3, dtype=float)
                if self.show_ppg_ir.get():
                    self.ax.plot(t, ir, label="IR PPG",
                                 color=C["accent4"], linewidth=1.0)
                need_bpm = (
                    (self.show_ppg_bpm.get() and not np.isnan(bpm).all()) or
                    (self.show_ppg_avg_bpm.get() and not np.isnan(avg).all()))
                if need_bpm:
                    ax2 = self.ax.twinx()
                    ax2.set_facecolor(C["bg_root"])
                    ax2.tick_params(colors=C["text_secondary"])
                    ax2.yaxis.label.set_color(C["text_secondary"])
                    if self.show_ppg_bpm.get() and not np.isnan(bpm).all():
                        ax2.plot(t, bpm, label="BPM",
                                 color=C["accent2"], linestyle="--", linewidth=1.0)
                    if self.show_ppg_avg_bpm.get() and not np.isnan(avg).all():
                        ax2.plot(t, avg, label="Avg BPM",
                                 color=C["accent3"], linestyle=":", linewidth=1.2)
                    ax2.set_ylabel("BPM", color=C["text_secondary"])
                    self.apply_auto_y_axis(ax2, [bpm, avg])
                    ax2.legend(loc="upper right",
                               facecolor=C["bg_card"], edgecolor=C["border"],
                               labelcolor=C["text_primary"], fontsize=8)
                self.ax.set_title(f"Realtime PPG | samples={len(t)}", color=C["accent4"])
                self.ax.set_xlabel("Thời gian (s)")
                self.ax.set_ylabel("IR Value")
                self.ax.set_xlim(0, max(1, self.realtime_xmax))
                self.apply_auto_y_axis(self.ax, [ir])

            self.ax.grid(True, linestyle="--", alpha=0.18, color=C["border_bright"])
            handles, labels = self.ax.get_legend_handles_labels()
            if handles:
                self.ax.legend(loc="upper left",
                               facecolor=C["bg_card"], edgecolor=C["border"],
                               labelcolor=C["text_primary"], fontsize=8)
            self.figure.tight_layout()
            self.canvas.draw_idle()

        if self.is_measuring:
            self.root.after(250, self.update_realtime_plot)

    def stop_realtime_plot(self):
        self.live_plot_running = False

    # ── MEASURE ──────────────────────────────────────────
    def start_measure_ecg(self):
        if self.is_measuring:
            messagebox.showwarning("Đang đo", "Đang có phiên đo khác.")
            return
        self.measure_thread = threading.Thread(
            target=lambda: self.measure_sensor_worker("EEG"), daemon=True)
        self.measure_thread.start()

    def start_measure_ppg(self):
        if self.is_measuring:
            messagebox.showwarning("Đang đo", "Đang có phiên đo khác.")
            return
        self.measure_thread = threading.Thread(
            target=lambda: self.measure_sensor_worker("PPG"), daemon=True)
        self.measure_thread.start()

    def start_measure_both(self):
        if self.is_measuring:
            messagebox.showwarning("Đang đo", "Đang có phiên đo khác.")
            return
        self.measure_thread = threading.Thread(
            target=lambda: self.measure_sensor_worker("BOTH"), daemon=True)
        self.measure_thread.start()

    def stop_measurement(self):
        self.is_measuring = False
        if self.serial_obj and self.serial_obj.is_open:
            try:
                self.send_mode(self.serial_obj, "IDLE")
            except Exception:
                pass
        self.set_status("[STOP] Đã yêu cầu dừng đo.")

    def get_record_time(self, var, default):
        try:
            value = int(var.get().strip())
            return value if value > 0 else default
        except Exception:
            return default

    def set_progress_safe(self, value, maximum=None):
        def _update():
            if maximum is not None:
                self.progress["maximum"] = maximum
            self.progress["value"] = value
        self.root.after(0, _update)

    def set_status(self, text):
        self.root.after(0, lambda: self.status_var.set(text))

    def measure_sensor_worker(self, mode="EEG"):
        self.is_measuring = True
        person_name = self.person_var.get().strip() or "unknown"
        record_time = self.get_record_time(
            self.ppg_time_var if mode == "PPG" else self.ecg_time_var,
            PPG_DEFAULT_RECORD_TIME if mode == "PPG" else ECG_DEFAULT_RECORD_TIME)
        timestamp = datetime.now()
        time_text = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
        ecg_file = ECG_DIR / f"{safe_filename(person_name)}_EEG_do_vao_{time_text}.csv"
        ppg_file = PPG_DIR / f"{safe_filename(person_name)}_PPG_do_vao_{time_text}.csv"

        eeg_timestamps = []
        ppg_timestamps = []
        red_values = []
        ir_values = []
        eeg_values = []

        try:
            self.serial_obj = self.open_serial()
            self.send_mode(self.serial_obj, mode)
            warmup_time = PPG_WARMUP_TIME if mode in ("PPG", "BOTH") else 0
            self.set_status(f"[UART] Đang thu mode {mode} cho {person_name}...")
            self.set_progress_safe(0, record_time)
            self.realtime_xmax = record_time + warmup_time
            plot_mode = "PPG" if mode == "PPG" else "ECG"
            self.root.after(0, lambda: self.reset_realtime_plot(plot_mode))
            self.root.after(300, self.update_realtime_plot)

            if warmup_time > 0:
                self.set_status(f"[UART {mode}] Cho PPG on dinh {warmup_time}s...")
                self.set_progress_safe(0, warmup_time)
                warmup_start = time.time()
                while self.is_measuring and time.time() - warmup_start < warmup_time:
                    self.set_progress_safe(time.time() - warmup_start)
                    time.sleep(0.05)
                self.set_status(f"[UART] Dang thu mode {mode} cho {person_name}...")
                self.set_progress_safe(0, record_time)

            start_time = time.time()
            while self.is_measuring and time.time() - start_time < record_time:
                line = self.serial_obj.readline().decode("utf-8", errors="ignore").strip()
                parsed = parse_sensor_line(line)
                if parsed is None:
                    continue
                red, ir, eeg = parsed
                if mode == "EEG" and np.isnan(eeg):
                    continue
                if mode == "PPG" and (np.isnan(red) or np.isnan(ir)):
                    continue
                if mode == "BOTH" and (np.isnan(red) or np.isnan(ir) or np.isnan(eeg)):
                    continue

                elapsed = time.time() - start_time
                if mode in ("EEG", "BOTH"):
                    eeg_timestamps.append(elapsed)
                    eeg_values.append(eeg)
                    self.append_realtime_ecg(elapsed, eeg, np.nan)
                if mode in ("PPG", "BOTH"):
                    ppg_timestamps.append(elapsed)
                    red_values.append(red)
                    ir_values.append(ir)
                    if mode == "PPG":
                        self.append_realtime_ppg(elapsed, red, ir, np.nan, np.nan)
                self.set_progress_safe(elapsed)
                self.set_status(
                    f"[UART {mode}] {elapsed:.1f}/{record_time}s "
                    f"| EEG={'' if np.isnan(eeg) else f'{eeg:.0f}'} "
                    f"| IR={'' if np.isnan(ir) else f'{ir:.0f}'}")

            self.send_mode(self.serial_obj, "IDLE")
            self.serial_obj.close()

            if mode in ("EEG", "BOTH") and not eeg_values:
                self.set_status("[ERROR] Không thu được dữ liệu EEG.")
                messagebox.showerror("UART Error", "Không thu được dữ liệu EEG.")
                return
            if mode in ("PPG", "BOTH") and not ir_values:
                self.set_status("[ERROR] Không thu được dữ liệu PPG.")
                messagebox.showerror("UART Error", "Không thu được dữ liệu PPG.")
                return

            saved_ecg = saved_ppg = None
            if mode in ("EEG", "BOTH"):
                saved_ecg = self.save_ecg_csv(
                    ecg_file, person_name, eeg_values, [np.nan] * len(eeg_values))
            if mode in ("PPG", "BOTH"):
                saved_ppg = self.save_ppg_csv(
                    ppg_file, person_name, ppg_timestamps,
                    red_values, ir_values,
                    [np.nan] * len(ir_values), [np.nan] * len(ir_values))

            saved_names = [p.name for p in (saved_ecg, saved_ppg) if p is not None]
            self.set_status(f"[SAVE] Đã lưu: {' | '.join(saved_names)}")
            self.refresh_file_list_safe()
            if self.plot_after_var.get():
                target_file = saved_ppg if mode == "PPG" else saved_ecg
                target_type = "PPG" if mode == "PPG" else "ECG"
                self.root.after(0, lambda: self.load_file_to_plot(target_file, target_type))
        except Exception as e:
            self.set_status(f"[ERROR] UART: {e}")
            self.root.after(0, lambda: messagebox.showerror("UART Error", str(e)))
        finally:
            self.is_measuring = False
            self.root.after(0, self.stop_realtime_plot)
            self.set_progress_safe(0)

    def save_ecg_csv(self, csv_file, person_name, raw_values, filtered_values):
        time_array = np.arange(len(raw_values)) / ECG_SAMPLE_RATE
        data_raw = np.array(raw_values, dtype=float)
        data_filtered_from_board = np.array(filtered_values, dtype=float)
        data_moving_avg = moving_average_filter(data_raw, WINDOW_SIZE)
        data_wavelet = wavelet_denoise(data_raw, WAVELET_NAME, WAVELET_LEVEL)
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "signal_type", "person_name", "sample_index", "time_s",
                "raw_voltage_v", "filtered_voltage_from_board_v",
                f"moving_average_w{WINDOW_SIZE}_v",
                f"wavelet_{WAVELET_NAME}_level{WAVELET_LEVEL}_v"])
            for i in range(len(data_raw)):
                writer.writerow([
                    "ECG", person_name, i,
                    f"{time_array[i]:.6f}",
                    f"{data_raw[i]:.6f}",
                    "" if np.isnan(data_filtered_from_board[i])
                    else f"{data_filtered_from_board[i]:.6f}",
                    f"{data_moving_avg[i]:.6f}",
                    "" if np.isnan(data_wavelet[i])
                    else f"{data_wavelet[i]:.6f}"])
        return csv_file

    def measure_ppg_worker(self):
        self.is_measuring = True
        person_name = self.person_var.get().strip() or "unknown"
        record_time = self.get_record_time(self.ppg_time_var, PPG_DEFAULT_RECORD_TIME)
        timestamp = datetime.now()
        time_text = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
        csv_file = PPG_DIR / f"{safe_filename(person_name)}_PPG_do_vao_{time_text}.csv"
        red_values = ir_values = bpm_values = avg_bpm_values = timestamps_s = []
        try:
            self.serial_obj = self.open_serial()
            self.send_mode(self.serial_obj, "PPG")
            self.realtime_xmax = PPG_WARMUP_TIME + record_time
            self.root.after(0, lambda: self.reset_realtime_plot("PPG"))
            self.root.after(300, self.update_realtime_plot)
            self.set_status(f"[PPG] Warm-up {PPG_WARMUP_TIME}s...")
            self.set_progress_safe(0, PPG_WARMUP_TIME)
            warmup_start = time.time()
            while self.is_measuring and time.time() - warmup_start < PPG_WARMUP_TIME:
                line = self.serial_obj.readline().decode("utf-8", errors="ignore").strip()
                parsed = parse_ppg_line(line)
                elapsed = time.time() - warmup_start
                self.set_progress_safe(elapsed)
                if parsed is not None:
                    red, ir, bpm, avg_bpm = parsed
                    self.append_realtime_ppg(elapsed, red, ir, bpm, avg_bpm)
            self.set_status(f"[PPG] Bắt đầu lưu dữ liệu cho {person_name}...")
            self.set_progress_safe(0, record_time)
            start_time = time.time()
            while self.is_measuring and time.time() - start_time < record_time:
                line = self.serial_obj.readline().decode("utf-8", errors="ignore").strip()
                parsed = parse_ppg_line(line)
                if parsed is None:
                    continue
                red, ir, bpm, avg_bpm = parsed
                elapsed = time.time() - start_time
                timestamps_s.append(elapsed)
                red_values.append(red)
                ir_values.append(ir)
                bpm_values.append(bpm)
                avg_bpm_values.append(avg_bpm)
                self.append_realtime_ppg(PPG_WARMUP_TIME + elapsed, red, ir, bpm, avg_bpm)
                self.set_progress_safe(elapsed)
            self.send_mode(self.serial_obj, "STOP")
            self.serial_obj.close()
            if not ir_values:
                self.set_status("[ERROR] Không thu được dữ liệu PPG.")
                messagebox.showerror("PPG Error", "Không thu được dữ liệu PPG.")
                return
            saved = self.save_ppg_csv(
                csv_file, person_name, timestamps_s,
                red_values, ir_values, bpm_values, avg_bpm_values)
            self.set_status(f"[SAVE] Đã lưu PPG: {saved.name}")
            self.refresh_file_list_safe()
            if self.plot_after_var.get():
                self.root.after(0, lambda: self.load_file_to_plot(saved, "PPG"))
        except Exception as e:
            self.set_status(f"[ERROR] PPG: {e}")
            self.root.after(0, lambda: messagebox.showerror("PPG Error", str(e)))
        finally:
            self.is_measuring = False
            self.root.after(0, self.stop_realtime_plot)
            self.set_progress_safe(0)

    def save_ppg_csv(self, csv_file, person_name, timestamps_s,
                     red_values, ir_values, bpm_values, avg_bpm_values):
        ir = np.array(ir_values, dtype=float)
        bpm = np.array(bpm_values, dtype=float)
        avg_bpm = np.array(avg_bpm_values, dtype=float)
        estimated_bpm, estimated_avg_bpm = estimate_bpm_from_ir(ir, PPG_SAMPLE_RATE)
        if np.isnan(bpm).all():
            bpm = estimated_bpm
        if np.isnan(avg_bpm).all():
            avg_bpm = estimated_avg_bpm
        ir_wavelet = wavelet_denoise(ir, WAVELET_NAME, WAVELET_LEVEL)
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "signal_type", "person_name", "sample_index",
                "time_s", "ir", "bpm", "avg_bpm",
                f"ir_wavelet_{WAVELET_NAME}_level{WAVELET_LEVEL}"])
            for i in range(len(ir_values)):
                writer.writerow([
                    "PPG", person_name, i,
                    f"{timestamps_s[i]:.6f}",
                    f"{ir[i]:.0f}",
                    "" if np.isnan(bpm[i]) else f"{bpm[i]:.6f}",
                    "" if np.isnan(avg_bpm[i]) else f"{avg_bpm[i]:.6f}",
                    "" if np.isnan(ir_wavelet[i]) else f"{ir_wavelet[i]:.6f}"])
        return csv_file

    # ── FILE LIST + PLOT ──────────────────────────────────
    def refresh_file_list_safe(self):
        self.root.after(0, self.refresh_file_list)

    def on_signal_change(self):
        self.refresh_file_list()
        self.replot_current()

    def list_csv_files(self):
        signal = self.signal_var.get()
        folder = ECG_DIR if signal == "ECG" else PPG_DIR
        files = sorted(folder.glob("*.csv"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        raw_keyword = self.search_var.get().strip()
        keyword = safe_filename(raw_keyword)
        if raw_keyword:
            files = [p for p in files if keyword in safe_filename(p.stem)]
        return files

    def refresh_file_list(self):
        self.current_files = self.list_csv_files()
        self.file_listbox.delete(0, tk.END)
        for f in self.current_files:
            self.file_listbox.insert(tk.END, f.name)
        if self.current_files:
            self.current_file_index = min(
                self.current_file_index, len(self.current_files) - 1)
            self.file_listbox.selection_clear(0, tk.END)
            self.file_listbox.selection_set(self.current_file_index)
            self.file_listbox.see(self.current_file_index)
        else:
            self.clear_plot("Không có file CSV phù hợp.")

    def on_file_selected(self, event=None):
        selection = self.file_listbox.curselection()
        if not selection:
            return
        self.current_file_index = selection[0]
        self.replot_current()

    def prev_file(self):
        if not self.current_files:
            return
        self.current_file_index = (self.current_file_index - 1) % len(self.current_files)
        self.file_listbox.selection_clear(0, tk.END)
        self.file_listbox.selection_set(self.current_file_index)
        self.file_listbox.see(self.current_file_index)
        self.replot_current()

    def next_file(self):
        if not self.current_files:
            return
        self.current_file_index = (self.current_file_index + 1) % len(self.current_files)
        self.file_listbox.selection_clear(0, tk.END)
        self.file_listbox.selection_set(self.current_file_index)
        self.file_listbox.see(self.current_file_index)
        self.replot_current()

    def load_file_to_plot(self, csv_file: Path, signal_type: str):
        self.signal_var.set(signal_type)
        self.search_var.set("")
        self.refresh_file_list()
        for idx, f in enumerate(self.current_files):
            if f.resolve() == csv_file.resolve():
                self.current_file_index = idx
                break
        self.file_listbox.selection_clear(0, tk.END)
        self.file_listbox.selection_set(self.current_file_index)
        self.file_listbox.see(self.current_file_index)
        self.replot_current()

    def replot_current(self):
        if not self.current_files:
            return
        csv_file = self.current_files[self.current_file_index]
        if self.signal_var.get() == "ECG":
            self.plot_ecg_file(csv_file)
        else:
            self.plot_ppg_file(csv_file)

    # ── FIXED AXIS ───────────────────────────────────────
    def get_fixed_xmax_for_current_files(self):
        if not self.current_files:
            return 1.0
        durations = [csv_duration_s(f) for f in self.current_files]
        xmax = max(durations) if durations else 1.0
        if xmax <= 10:   return 10.0
        if xmax <= 20:   return 20.0
        if xmax <= 30:   return 30.0
        if xmax <= 60:   return 60.0
        return float(np.ceil(xmax / 30.0) * 30.0)

    def apply_fixed_x_axis(self):
        xmax = self.get_fixed_xmax_for_current_files()
        self.ax.set_xlim(0, xmax)

    def apply_auto_y_axis(self, axis, arrays, padding_ratio=0.12):
        values = []
        for arr in arrays:
            data = np.array(arr, dtype=float)
            data = data[np.isfinite(data)]
            if data.size:
                values.append(data)
        if not values:
            return
        data = np.concatenate(values)
        if data.size >= 20:
            low, high = np.percentile(data, [1, 99])
        else:
            low, high = np.min(data), np.max(data)
        if not np.isfinite(low) or not np.isfinite(high):
            return
        margin = (high - low) * padding_ratio if high > low else max(1.0, abs(high) * 0.05)
        axis.set_ylim(low - margin, high + margin)

    # ── AXIS STYLING ─────────────────────────────────────
    def _style_axis_base(self):
        self.ax.set_facecolor(C["bg_root"])
        self.ax.tick_params(colors=C["text_secondary"], labelsize=8)
        self.ax.xaxis.label.set_color(C["text_secondary"])
        self.ax.yaxis.label.set_color(C["text_secondary"])
        self.ax.title.set_color(C["accent"])
        for spine in self.ax.spines.values():
            spine.set_color(C["border"])
        self.figure.patch.set_facecolor(C["bg_card"])

    def style_axis(self):
        self._style_axis_base()

    def clear_plot(self, text):
        self.ax.clear()
        self._style_axis_base()
        self.ax.set_title(text, color=C["text_secondary"])
        self.canvas.draw_idle()

    # ── PLOT ECG ─────────────────────────────────────────
    def plot_ecg_file(self, csv_path: Path):
        try:
            rows = read_csv_dicts(csv_path)
            if not rows:
                self.clear_plot("File ECG rỗng.")
                return
            person = rows[0].get("person_name", "unknown")
            t = to_float_array(rows, "time_s")
            raw = to_float_array(rows, "raw_voltage_v")
            board_filtered = to_float_array(rows, "filtered_voltage_from_board_v")
            ma_key = next((k for k in rows[0].keys() if k.startswith("moving_average")), None)
            wavelet_key = next((k for k in rows[0].keys() if k.startswith("wavelet")), None)

            for extra_ax in list(self.figure.axes)[1:]:
                extra_ax.remove()
            self.ax.clear()
            self._style_axis_base()
            ecg_plot_values = []

            if self.show_ecg_raw.get():
                self.ax.plot(t, raw, label="Raw ECG",
                             color=C["accent5"], alpha=0.50, linewidth=0.9)
                ecg_plot_values.append(raw)

            if self.show_ecg_filtered.get() and not np.isnan(board_filtered).all():
                self.ax.plot(t, board_filtered, label="Filtered (ESP32)",
                             color=C["accent"], linewidth=1.2)
                ecg_plot_values.append(board_filtered)

            if self.show_ecg_ma.get() and ma_key:
                ma = to_float_array(rows, ma_key)
                if not np.isnan(ma).all():
                    self.ax.plot(t, ma, label=ma_key,
                                 color=C["accent4"], linewidth=1.1)
                    ecg_plot_values.append(ma)

            if self.show_ecg_wavelet.get() and wavelet_key:
                wv = to_float_array(rows, wavelet_key)
                if not np.isnan(wv).all():
                    self.ax.plot(t, wv, label=wavelet_key,
                                 color=C["accent2"], linewidth=1.1)
                    ecg_plot_values.append(wv)

            self.ax.set_title(
                f"ECG — {person}  |  {self.current_file_index + 1}/{len(self.current_files)}  |  {csv_path.name}",
                color=C["accent"], fontsize=9)
            self.ax.set_xlabel("Thời gian (s)")
            self.ax.set_ylabel("Điện áp (V)")
            self.apply_fixed_x_axis()
            self.apply_auto_y_axis(self.ax, ecg_plot_values)
            self.ax.grid(True, linestyle="--", alpha=0.18, color=C["border_bright"])
            handles, labels = self.ax.get_legend_handles_labels()
            if handles:
                self.ax.legend(facecolor=C["bg_card"], edgecolor=C["border"],
                               labelcolor=C["text_primary"], fontsize=8)
            self.figure.tight_layout()
            self.canvas.draw_idle()
            self.set_status(f"[PLOT] ECG: {csv_path.name}")
        except Exception as e:
            self.clear_plot(f"Lỗi plot ECG: {e}")

    # ── PLOT PPG ─────────────────────────────────────────
    def plot_ppg_file(self, csv_path: Path):
        try:
            rows = read_csv_dicts(csv_path)
            if not rows:
                self.clear_plot("File PPG rỗng.")
                return
            person = rows[0].get("person_name", "unknown")
            t = to_float_array(rows, "time_s")
            ir = to_float_array(rows, "ir")
            bpm = to_float_array(rows, "bpm")
            avg_bpm = to_float_array(rows, "avg_bpm")
            ir_wavelet_key = next((k for k in rows[0].keys() if k.startswith("ir_wavelet")), None)

            for extra_ax in list(self.figure.axes)[1:]:
                extra_ax.remove()
            self.ax.clear()
            self._style_axis_base()

            view = self.ppg_view_var.get()

            if view == "BPM":
                if self.show_ppg_bpm.get() and not np.isnan(bpm).all():
                    self.ax.plot(t, bpm, label="BPM",
                                 color=C["accent2"], linewidth=1.2)
                if self.show_ppg_avg_bpm.get() and not np.isnan(avg_bpm).all():
                    self.ax.plot(t, avg_bpm, label="Avg BPM",
                                 color=C["accent3"], linewidth=1.5)
                self.ax.set_title(
                    f"PPG BPM — {person}  |  {self.current_file_index + 1}/{len(self.current_files)}  |  {csv_path.name}",
                    color=C["accent4"], fontsize=9)
                self.ax.set_ylabel("BPM")
                self.apply_auto_y_axis(self.ax, [bpm, avg_bpm])

            else:
                ppg_plot_values = []
                if self.show_ppg_ir.get():
                    self.ax.plot(t, ir, label="IR PPG",
                                 color=C["accent4"], alpha=0.55, linewidth=0.9)
                    ppg_plot_values.append(ir)
                if self.show_ppg_wavelet.get():
                    if ir_wavelet_key:
                        ir_wavelet = to_float_array(rows, ir_wavelet_key)
                        if not np.isnan(ir_wavelet).all():
                            self.ax.plot(t, ir_wavelet, label=ir_wavelet_key,
                                         color=C["accent"], linewidth=1.3)
                            ppg_plot_values.append(ir_wavelet)
                self.ax.set_title(
                    f"PPG IR — {person}  |  {self.current_file_index + 1}/{len(self.current_files)}  |  {csv_path.name}",
                    color=C["accent4"], fontsize=9)
                self.ax.set_ylabel("PPG Value")
                self.apply_auto_y_axis(self.ax, ppg_plot_values)

            self.ax.set_xlabel("Thời gian (s)")
            self.apply_fixed_x_axis()
            self.ax.grid(True, linestyle="--", alpha=0.18, color=C["border_bright"])
            handles, labels = self.ax.get_legend_handles_labels()
            if handles:
                self.ax.legend(facecolor=C["bg_card"], edgecolor=C["border"],
                               labelcolor=C["text_primary"], fontsize=8)
            self.figure.tight_layout()
            self.canvas.draw_idle()
            self.set_status(f"[PLOT] PPG: {csv_path.name}")
        except Exception as e:
            self.clear_plot(f"Lỗi plot PPG: {e}")


def main():
    root = tk.Tk()
    app = HeartSensorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
