import csv
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

try:
    import pywt
except ImportError:
    pywt = None

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure


ECG_SAMPLE_RATE_HZ = 1000.0
ECG_HIGHPASS_HZ = 0.5
ECG_LOWPASS_HZ = 45.0
ECG_MAINS_HZ = 50.0
ECG_MAINS_NOTCH_WIDTH_HZ = 2.0

BASE_DIR = Path("data_csv") / "SYNC"
RAW_DIR = BASE_DIR / "raw"
COMPARE_DIR = BASE_DIR / "wavelet_compare"
COMPARE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_WAVELETS = [
    "haar", "db2", "db4", "db6", "db8",
    "sym4", "sym6", "sym8",
    "coif1", "coif3", "coif5",
    "bior3.5", "bior4.4",
    "rbio3.5", "rbio4.4",
]

SIGNAL_COLUMNS = {
    "ECG raw": "ecg_raw",
    "PPG IR raw": "ppg_ir_raw",
    "PPG RED raw": "ppg_red_raw",
}


@dataclass
class SyncRow:
    time_ms: int
    ecg_raw: float
    ppg_red_raw: float
    ppg_ir_raw: float


@dataclass
class FilterResult:
    wavelet: str
    level: int
    values: np.ndarray
    noise_std: float
    residual_rms: float
    peak_to_peak: float


def safe_filename(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", text)
    return text.strip("_") or "unknown"


def parse_float_or_nan(text: str) -> float:
    text = text.strip()
    if not text:
        return np.nan
    return float(text)


def parse_sync_csv_line(line: str) -> SyncRow | None:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return SyncRow(
            time_ms=int(float(parts[0])),
            ecg_raw=parse_float_or_nan(parts[1]),
            ppg_red_raw=parse_float_or_nan(parts[2]),
            ppg_ir_raw=parse_float_or_nan(parts[3]),
        )
    except ValueError:
        return None


def normalize_rows(rows: list[SyncRow]) -> list[SyncRow]:
    return [row for _, row in sorted(enumerate(rows), key=lambda item: (item[1].time_ms, item[0]))]


def load_raw_rows(path: Path) -> list[SyncRow]:
    rows: list[SyncRow] = []
    in_uart_block = False
    saw_uart_marker = False

    with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            upper = line.upper()
            if upper.startswith("BEGIN_SYNC_CSV"):
                saw_uart_marker = True
                in_uart_block = True
                rows.clear()
                continue
            if upper.startswith("END_SYNC_CSV"):
                break
            if upper.startswith("STATS"):
                continue
            if upper.startswith("TIME_MS"):
                continue

            if saw_uart_marker and not in_uart_block:
                continue

            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5 and parts[1].lower() == "time_ms":
                continue
            if len(parts) >= 5:
                line = ",".join(parts[1:5])

            row = parse_sync_csv_line(line)
            if row is not None:
                rows.append(row)

    return normalize_rows(rows)


def rows_to_signal(rows: list[SyncRow], column_name: str) -> tuple[np.ndarray, np.ndarray]:
    time_ms: list[float] = []
    values: list[float] = []
    for row in rows:
        value = getattr(row, column_name)
        if np.isfinite(value):
            time_ms.append(float(row.time_ms))
            values.append(float(value))
    return np.asarray(time_ms, dtype=float), np.asarray(values, dtype=float)


def fft_ecg_band_clean(values: np.ndarray, sample_rate_hz: float = ECG_SAMPLE_RATE_HZ) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < 8:
        return values.copy()

    centered = values - float(np.nanmedian(values))
    spectrum = np.fft.rfft(centered)
    freqs = np.fft.rfftfreq(len(centered), d=1.0 / sample_rate_hz)

    keep = (freqs >= ECG_HIGHPASS_HZ) & (freqs <= ECG_LOWPASS_HZ)
    notch = np.abs(freqs - ECG_MAINS_HZ) <= (ECG_MAINS_NOTCH_WIDTH_HZ / 2.0)
    spectrum[~keep | notch] = 0
    return np.fft.irfft(spectrum, n=len(centered))


def preprocess_signal(values: np.ndarray, mode: str) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if mode == "Center median":
        return values - float(np.nanmedian(values))
    if mode == "ECG FFT + center":
        return fft_ecg_band_clean(values)
    return values.copy()


def wavelet_denoise(
    values: np.ndarray,
    wavelet_name: str,
    requested_level: int,
    threshold_mode: str,
    threshold_scale: float,
) -> tuple[np.ndarray, int]:
    if pywt is None:
        raise RuntimeError("Can cai PyWavelets: pip install PyWavelets")

    values = np.asarray(values, dtype=float)
    if len(values) < 8:
        return values.copy(), 0

    wavelet = pywt.Wavelet(wavelet_name)
    max_level = pywt.dwt_max_level(data_len=len(values), filter_len=wavelet.dec_len)
    level = min(max(requested_level, 1), max_level)
    if level < 1:
        return values.copy(), 0

    coeffs = pywt.wavedec(values, wavelet_name, mode="symmetric", level=level)
    detail = coeffs[-1]
    sigma = np.median(np.abs(detail)) / 0.6745 if len(detail) else 0.0
    threshold = threshold_scale * sigma * np.sqrt(2 * np.log(len(values)))

    filtered_coeffs = [coeffs[0]]
    for detail_coeff in coeffs[1:]:
        filtered_coeffs.append(pywt.threshold(detail_coeff, value=threshold, mode=threshold_mode.lower()))

    filtered = pywt.waverec(filtered_coeffs, wavelet_name, mode="symmetric")[:len(values)]
    return filtered, level


def compute_result(
    base_values: np.ndarray,
    wavelet: str,
    level: int,
    threshold_mode: str,
    threshold_scale: float,
) -> FilterResult:
    filtered, actual_level = wavelet_denoise(base_values, wavelet, level, threshold_mode, threshold_scale)
    residual = base_values - filtered
    return FilterResult(
        wavelet=wavelet,
        level=actual_level,
        values=filtered,
        noise_std=float(np.nanstd(residual)),
        residual_rms=float(np.sqrt(np.nanmean(residual ** 2))),
        peak_to_peak=float(np.nanmax(filtered) - np.nanmin(filtered)),
    )


class WaveletCompareUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Wavelet Filter Compare - Raw CSV")
        self.root.geometry("1220x780")
        self.root.minsize(1040, 660)

        self.rows: list[SyncRow] = []
        self.current_file: Path | None = None
        self.current_time_s = np.array([], dtype=float)
        self.current_raw = np.array([], dtype=float)
        self.current_base = np.array([], dtype=float)
        self.current_results: list[FilterResult] = []

        self.signal_var = tk.StringVar(value="ECG raw")
        self.preprocess_var = tk.StringVar(value="ECG FFT + center")
        self.level_var = tk.IntVar(value=3)
        self.threshold_var = tk.StringVar(value="Soft")
        self.threshold_scale_var = tk.DoubleVar(value=1.0)
        self.status_var = tk.StringVar(value="Open a raw CSV file to compare wavelet filters.")
        self.show_raw_var = tk.BooleanVar(value=True)
        self.show_base_var = tk.BooleanVar(value=False)
        self.wavelet_vars: dict[str, tk.BooleanVar] = {}

        self.build_ui()
        self.set_default_wavelets()

        if pywt is None:
            messagebox.showwarning("Missing PyWavelets", "Can cai PyWavelets de loc wavelet:\npip install PyWavelets")

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="Open raw CSV", command=self.open_file).grid(row=0, column=0, padx=(0, 8), sticky="w")
        ttk.Button(top, text="Run compare", command=self.run_compare).grid(row=0, column=1, padx=4, sticky="w")
        ttk.Button(top, text="Save compare CSV", command=self.save_compare_csv).grid(row=0, column=2, padx=4, sticky="w")

        ttk.Label(top, text="Signal").grid(row=0, column=3, padx=(18, 4), sticky="w")
        signal_combo = ttk.Combobox(top, textvariable=self.signal_var, values=list(SIGNAL_COLUMNS.keys()), width=13, state="readonly")
        signal_combo.grid(row=0, column=4, padx=4, sticky="w")
        signal_combo.bind("<<ComboboxSelected>>", lambda _event: self.run_compare())

        ttk.Label(top, text="Preprocess").grid(row=0, column=5, padx=(18, 4), sticky="w")
        preprocess_combo = ttk.Combobox(
            top,
            textvariable=self.preprocess_var,
            values=["Raw", "Center median", "ECG FFT + center"],
            width=16,
            state="readonly",
        )
        preprocess_combo.grid(row=0, column=6, padx=4, sticky="w")

        ttk.Label(top, text="Level").grid(row=0, column=7, padx=(18, 4), sticky="w")
        ttk.Spinbox(top, from_=1, to=8, textvariable=self.level_var, width=5).grid(row=0, column=8, padx=4, sticky="w")

        ttk.Label(top, text="Threshold").grid(row=0, column=9, padx=(18, 4), sticky="w")
        ttk.Combobox(top, textvariable=self.threshold_var, values=["Soft", "Hard"], width=7, state="readonly").grid(row=0, column=10, padx=4, sticky="w")

        ttk.Label(top, text="Scale").grid(row=0, column=11, padx=(18, 4), sticky="w")
        ttk.Spinbox(top, from_=0.2, to=3.0, increment=0.1, textvariable=self.threshold_scale_var, width=6).grid(row=0, column=12, padx=4, sticky="w")

        ttk.Checkbutton(top, text="Raw", variable=self.show_raw_var, command=self.plot_results).grid(row=1, column=0, pady=(8, 0), sticky="w")
        ttk.Checkbutton(top, text="Preprocessed", variable=self.show_base_var, command=self.plot_results).grid(row=1, column=1, pady=(8, 0), sticky="w")
        ttk.Label(top, textvariable=self.status_var).grid(row=1, column=2, columnspan=11, pady=(8, 0), sticky="ew")
        top.columnconfigure(12, weight=1)

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        side = ttk.Frame(body, padding=(0, 0, 8, 0))
        body.add(side, weight=1)

        wavelet_box = ttk.LabelFrame(side, text="Wavelets", padding=8)
        wavelet_box.pack(fill="both", expand=False)

        self.wavelet_frame = ttk.Frame(wavelet_box)
        self.wavelet_frame.pack(fill="both", expand=True)

        button_row = ttk.Frame(side)
        button_row.pack(fill="x", pady=(8, 8))
        ttk.Button(button_row, text="Select common", command=self.set_default_wavelets).pack(side="left", padx=(0, 4))
        ttk.Button(button_row, text="Select all", command=self.select_all_wavelets).pack(side="left", padx=4)
        ttk.Button(button_row, text="Clear", command=self.clear_wavelets).pack(side="left", padx=4)

        table_box = ttk.LabelFrame(side, text="Metrics", padding=8)
        table_box.pack(fill="both", expand=True)

        columns = ("wavelet", "level", "noise_std", "residual_rms", "peak_to_peak")
        self.metrics = ttk.Treeview(table_box, columns=columns, show="headings", height=12)
        headings = {
            "wavelet": "Wavelet",
            "level": "Level",
            "noise_std": "Noise std",
            "residual_rms": "Residual RMS",
            "peak_to_peak": "Peak-to-peak",
        }
        widths = {
            "wavelet": 80,
            "level": 50,
            "noise_std": 90,
            "residual_rms": 95,
            "peak_to_peak": 100,
        }
        for col in columns:
            self.metrics.heading(col, text=headings[col])
            self.metrics.column(col, width=widths[col], anchor="center")
        self.metrics.pack(fill="both", expand=True)

        plot_frame = ttk.Frame(body)
        body.add(plot_frame, weight=4)

        self.figure = Figure(figsize=(8.8, 5.8), dpi=100)
        self.ax_signal = self.figure.add_subplot(211)
        self.ax_residual = self.figure.add_subplot(212, sharex=self.ax_signal)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, plot_frame)

        self.create_wavelet_checks()

    def create_wavelet_checks(self):
        wavelets = DEFAULT_WAVELETS
        if pywt is not None:
            available = set(pywt.wavelist(kind="discrete"))
            wavelets = [name for name in DEFAULT_WAVELETS if name in available]

        for index, name in enumerate(wavelets):
            var = tk.BooleanVar(value=False)
            self.wavelet_vars[name] = var
            chk = ttk.Checkbutton(self.wavelet_frame, text=name, variable=var)
            chk.grid(row=index // 3, column=index % 3, sticky="w", padx=6, pady=3)

    def set_default_wavelets(self):
        defaults = {"haar", "db4", "db6", "sym4", "sym6", "coif3", "bior4.4"}
        for name, var in self.wavelet_vars.items():
            var.set(name in defaults)

    def select_all_wavelets(self):
        for var in self.wavelet_vars.values():
            var.set(True)

    def clear_wavelets(self):
        for var in self.wavelet_vars.values():
            var.set(False)

    def selected_wavelets(self) -> list[str]:
        return [name for name, var in self.wavelet_vars.items() if var.get()]

    def open_file(self):
        filename = filedialog.askopenfilename(
            title="Open raw ECG/PPG CSV",
            initialdir=str(RAW_DIR if RAW_DIR.exists() else Path.cwd()),
            filetypes=[
                ("CSV/Text files", "*.csv *.txt *.log"),
                ("All files", "*.*"),
            ],
        )
        if not filename:
            return

        path = Path(filename)
        try:
            rows = load_raw_rows(path)
            if not rows:
                raise ValueError("Khong tim thay du lieu raw hop le.")
            self.rows = rows
            self.current_file = path
            self.status_var.set(f"Loaded {path.name} | rows={len(rows)}")
            self.run_compare()
        except Exception as exc:
            messagebox.showerror("Open CSV Error", str(exc))
            self.status_var.set(f"ERROR: {exc}")

    def run_compare(self):
        if not self.rows:
            return
        if pywt is None:
            messagebox.showerror("Missing PyWavelets", "Can cai PyWavelets:\npip install PyWavelets")
            return

        wavelets = self.selected_wavelets()
        if not wavelets:
            messagebox.showwarning("Wavelet", "Hay chon it nhat mot wavelet.")
            return

        column = SIGNAL_COLUMNS[self.signal_var.get()]
        time_ms, raw = rows_to_signal(self.rows, column)
        if len(raw) < 8:
            messagebox.showwarning("Signal", "Tin hieu qua ngan hoac cot dang chon khong co du lieu.")
            return

        base = preprocess_signal(raw, self.preprocess_var.get())
        level = int(self.level_var.get())
        threshold_mode = self.threshold_var.get()
        threshold_scale = float(self.threshold_scale_var.get())

        results: list[FilterResult] = []
        failed: list[str] = []
        for wavelet in wavelets:
            try:
                results.append(compute_result(base, wavelet, level, threshold_mode, threshold_scale))
            except Exception as exc:
                failed.append(f"{wavelet}: {exc}")

        self.current_time_s = time_ms / 1000.0
        self.current_raw = raw
        self.current_base = base
        self.current_results = results
        self.update_metrics()
        self.plot_results()

        status = f"{self.signal_var.get()} | samples={len(raw)} | filters={len(results)}"
        if failed:
            status += f" | failed={len(failed)}"
        self.status_var.set(status)
        if failed:
            messagebox.showwarning("Some wavelets failed", "\n".join(failed[:8]))

    def update_metrics(self):
        for item in self.metrics.get_children():
            self.metrics.delete(item)
        for result in sorted(self.current_results, key=lambda item: item.residual_rms):
            self.metrics.insert(
                "",
                "end",
                values=(
                    result.wavelet,
                    result.level,
                    f"{result.noise_std:.3f}",
                    f"{result.residual_rms:.3f}",
                    f"{result.peak_to_peak:.3f}",
                ),
            )

    def plot_results(self):
        self.ax_signal.clear()
        self.ax_residual.clear()

        if len(self.current_time_s) == 0:
            self.ax_signal.set_title("Open a raw CSV file")
            self.canvas.draw_idle()
            return

        t = self.current_time_s
        raw_plot = self.current_raw
        base_plot = self.current_base

        if self.show_raw_var.get():
            raw_centered = raw_plot - float(np.nanmedian(raw_plot))
            self.ax_signal.plot(t, raw_centered, color="#8a8a8a", linewidth=0.8, alpha=0.55, label="raw centered")
        if self.show_base_var.get():
            self.ax_signal.plot(t, base_plot, color="#111111", linewidth=0.9, alpha=0.7, label="preprocessed")

        for result in self.current_results:
            self.ax_signal.plot(t, result.values, linewidth=1.0, label=f"{result.wavelet} L{result.level}")
            residual = base_plot - result.values
            self.ax_residual.plot(t, residual, linewidth=0.8, alpha=0.75, label=result.wavelet)

        title = self.current_file.name if self.current_file else "Wavelet compare"
        self.ax_signal.set_title(f"{title} - {self.signal_var.get()}")
        self.ax_signal.set_ylabel("Amplitude")
        self.ax_signal.grid(True, alpha=0.22)
        self.ax_signal.legend(loc="upper right", fontsize=8, ncol=2)

        self.ax_residual.set_title("Residual: preprocessed - filtered")
        self.ax_residual.set_xlabel("Time (s)")
        self.ax_residual.set_ylabel("Residual")
        self.ax_residual.grid(True, alpha=0.22)
        self.ax_residual.legend(loc="upper right", fontsize=8, ncol=2)

        self.figure.tight_layout()
        self.canvas.draw_idle()

    def save_compare_csv(self):
        if len(self.current_time_s) == 0 or not self.current_results:
            messagebox.showwarning("Save", "Chua co ket qua de luu.")
            return

        source = self.current_file.stem if self.current_file else "raw"
        signal = safe_filename(self.signal_var.get())
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        default_path = COMPARE_DIR / f"{source}_{signal}_wavelet_compare_{ts}.csv"

        filename = filedialog.asksaveasfilename(
            title="Save wavelet comparison CSV",
            initialdir=str(COMPARE_DIR),
            initialfile=default_path.name,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return

        path = Path(filename)
        headers = ["time_s", "raw", "preprocessed"]
        headers.extend([f"{result.wavelet}_L{result.level}" for result in self.current_results])
        headers.extend([f"residual_{result.wavelet}_L{result.level}" for result in self.current_results])

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for i, time_s in enumerate(self.current_time_s):
                row = [
                    f"{time_s:.6f}",
                    f"{self.current_raw[i]:.6f}",
                    f"{self.current_base[i]:.6f}",
                ]
                row.extend(f"{result.values[i]:.6f}" for result in self.current_results)
                row.extend(f"{(self.current_base[i] - result.values[i]):.6f}" for result in self.current_results)
                writer.writerow(row)

        self.status_var.set(f"Saved comparison: {path}")


def main():
    root = tk.Tk()
    WaveletCompareUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
