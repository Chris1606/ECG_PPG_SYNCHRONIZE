import csv
import sys
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np

try:
    import pywt
    HAS_PYWT = True
except ImportError:
    HAS_PYWT = False

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure


BASE_DIR = Path("data_csv") / "SYNC"
RAW_DIR = BASE_DIR / "raw"
COMPARE_DIR = BASE_DIR / "wavelet_compare"
COMPARE_DIR.mkdir(parents=True, exist_ok=True)

ECG_SAMPLE_RATE_HZ = 1000.0
ECG_HIGHPASS_HZ = 0.5
ECG_LOWPASS_HZ = 45.0
ECG_MAINS_HZ = 50.0
ECG_MAINS_NOTCH_WIDTH_HZ = 2.0

DEFAULT_WAVELETS = ["db4", "sym2", "sym4", "coif1", "bior3.5"]


@dataclass
class SyncRow:
    time_ms: int
    ecg_raw: float
    ppg_red_raw: float
    ppg_ir_raw: float


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


def normalize_sync_rows(rows: list[SyncRow]) -> list[SyncRow]:
    return [row for _, row in sorted(enumerate(rows), key=lambda item: (item[1].time_ms, item[0]))]


def load_sync_rows_from_file(path: Path) -> list[SyncRow]:
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
            if upper.startswith("TIME_MS"):
                continue
            if saw_uart_marker and not in_uart_block:
                continue

            # Saved raw CSV: person_name,time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw
            # UART capture: time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5 and parts[1].lower() == "time_ms":
                continue
            if len(parts) >= 5:
                line = ",".join(parts[1:5])

            row = parse_sync_csv_line(line)
            if row is not None:
                rows.append(row)

    return normalize_sync_rows(rows)


def fft_ecg_band_clean(values: np.ndarray, sample_rate_hz: float = ECG_SAMPLE_RATE_HZ) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < 8:
        return values.copy()

    baseline = float(np.nanmedian(values))
    centered = values - baseline
    spectrum = np.fft.rfft(centered)
    freqs = np.fft.rfftfreq(len(centered), d=1.0 / sample_rate_hz)

    keep = (freqs >= ECG_HIGHPASS_HZ) & (freqs <= ECG_LOWPASS_HZ)
    notch = np.abs(freqs - ECG_MAINS_HZ) <= (ECG_MAINS_NOTCH_WIDTH_HZ / 2.0)
    spectrum[~keep | notch] = 0

    return np.fft.irfft(spectrum, n=len(centered))


def wavelet_denoise(
    values: np.ndarray,
    wavelet_name: str,
    level: int,
    threshold_mode: str,
) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < 8:
        return values.copy()

    wavelet = pywt.Wavelet(wavelet_name)
    max_level = pywt.dwt_max_level(data_len=len(values), filter_len=wavelet.dec_len)
    level = min(level, max_level)
    if level < 1:
        return values.copy()

    coeffs = pywt.wavedec(values, wavelet_name, mode="symmetric", level=level)
    sigma = np.median(np.abs(coeffs[-1])) / 0.6745 if len(coeffs[-1]) else 0.0
    threshold = sigma * np.sqrt(2 * np.log(len(values)))

    filtered_coeffs = [coeffs[0]]
    for detail in coeffs[1:]:
        filtered_coeffs.append(pywt.threshold(detail, value=threshold, mode=threshold_mode))

    return pywt.waverec(filtered_coeffs, wavelet_name, mode="symmetric")[:len(values)]


class WaveletComparatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Wavelet Filter Comparator")
        self.root.geometry("1180x780")
        self.root.minsize(960, 640)

        self.rows: list[SyncRow] = []
        self.current_file: Path | None = None
        self.last_results: dict[str, np.ndarray] = {}
        self.last_time_s = np.array([], dtype=float)
        self.last_raw = np.array([], dtype=float)

        self.signal_var = tk.StringVar(value="ECG")
        self.level_var = tk.IntVar(value=3)
        self.threshold_var = tk.StringVar(value="soft")
        self.show_raw_var = tk.BooleanVar(value=True)
        self.center_var = tk.BooleanVar(value=True)
        self.ecg_prefilter_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Open a raw CSV file to compare wavelet filters.")

        self.build_ui()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="Open Raw CSV", command=self.open_file).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(top, text="Plot Compare", command=self.plot_compare).grid(row=0, column=1, padx=4)
        ttk.Button(top, text="Export CSV", command=self.export_csv).grid(row=0, column=2, padx=4)

        ttk.Label(top, text="Signal").grid(row=0, column=3, padx=(18, 4), sticky="e")
        ttk.Combobox(
            top,
            textvariable=self.signal_var,
            values=["ECG", "PPG_IR", "PPG_RED"],
            width=10,
            state="readonly",
        ).grid(row=0, column=4, padx=4)

        ttk.Label(top, text="Level").grid(row=0, column=5, padx=(18, 4), sticky="e")
        ttk.Spinbox(top, from_=1, to=8, textvariable=self.level_var, width=5).grid(row=0, column=6, padx=4)

        ttk.Label(top, text="Threshold").grid(row=0, column=7, padx=(18, 4), sticky="e")
        ttk.Combobox(
            top,
            textvariable=self.threshold_var,
            values=["soft", "hard", "garrote"],
            width=9,
            state="readonly",
        ).grid(row=0, column=8, padx=4)

        opts = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        opts.pack(fill="x")
        ttk.Checkbutton(opts, text="Show raw as separate axis", variable=self.show_raw_var).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(opts, text="Center each signal by median", variable=self.center_var).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(opts, text="ECG FFT bandpass + 50Hz notch before wavelet", variable=self.ecg_prefilter_var).pack(side="left")

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        body.add(left, weight=1)

        ttk.Label(left, text="Wavelets").pack(anchor="w")
        self.wavelet_list = tk.Listbox(left, selectmode="extended", exportselection=False, height=22)
        self.wavelet_list.pack(fill="both", expand=True)

        scrollbar = ttk.Scrollbar(self.wavelet_list, orient="vertical", command=self.wavelet_list.yview)
        self.wavelet_list.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self.populate_wavelets()
        ttk.Button(left, text="Select Defaults", command=self.select_default_wavelets).pack(fill="x", pady=(8, 4))
        ttk.Button(left, text="Clear Selection", command=lambda: self.wavelet_list.selection_clear(0, "end")).pack(fill="x")

        plot_frame = ttk.Frame(body)
        body.add(plot_frame, weight=5)

        self.figure = Figure(figsize=(9, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, plot_frame)

        ttk.Label(self.root, textvariable=self.status_var, padding=(10, 0)).pack(fill="x")

    def populate_wavelets(self):
        if HAS_PYWT:
            names = pywt.wavelist(kind="discrete")
        else:
            names = DEFAULT_WAVELETS

        for name in names:
            self.wavelet_list.insert("end", name)
        self.select_default_wavelets()

    def select_default_wavelets(self):
        self.wavelet_list.selection_clear(0, "end")
        names = list(self.wavelet_list.get(0, "end"))
        for default in DEFAULT_WAVELETS:
            if default in names:
                self.wavelet_list.selection_set(names.index(default))

    def selected_wavelets(self) -> list[str]:
        return [self.wavelet_list.get(i) for i in self.wavelet_list.curselection()]

    def open_file(self):
        initial_dir = RAW_DIR if RAW_DIR.exists() else Path.cwd()
        filename = filedialog.askopenfilename(
            title="Choose raw ECG/PPG CSV",
            initialdir=str(initial_dir),
            filetypes=[
                ("CSV/Text files", "*.csv *.txt *.log"),
                ("All files", "*.*"),
            ],
        )
        if not filename:
            return

        path = Path(filename)
        try:
            rows = load_sync_rows_from_file(path)
            if not rows:
                raise ValueError("No valid time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw rows found.")
        except Exception as exc:
            messagebox.showerror("Open CSV Error", str(exc))
            self.status_var.set(f"ERROR: {exc}")
            return

        self.rows = rows
        self.current_file = path
        duration_s = (rows[-1].time_ms - rows[0].time_ms) / 1000.0 if len(rows) > 1 else 0.0
        self.status_var.set(f"Loaded {path.name}: {len(rows)} rows, duration {duration_s:.2f}s")
        self.plot_compare()

    def signal_values(self) -> tuple[np.ndarray, np.ndarray, str]:
        signal = self.signal_var.get()
        rows = normalize_sync_rows(self.rows)
        time_ms = np.array([row.time_ms for row in rows], dtype=float)

        if signal == "ECG":
            values = np.array([row.ecg_raw for row in rows], dtype=float)
            label = "ECG raw"
        elif signal == "PPG_RED":
            values = np.array([row.ppg_red_raw for row in rows], dtype=float)
            label = "PPG RED raw"
        else:
            values = np.array([row.ppg_ir_raw for row in rows], dtype=float)
            label = "PPG IR raw"

        mask = np.isfinite(values)
        return time_ms[mask] / 1000.0, values[mask], label

    def prepare_for_wavelet(self, values: np.ndarray) -> np.ndarray:
        signal = self.signal_var.get()
        prepared = np.asarray(values, dtype=float)
        if signal == "ECG" and self.ecg_prefilter_var.get():
            prepared = fft_ecg_band_clean(prepared)
        if self.center_var.get():
            prepared = prepared - np.nanmedian(prepared)
        return prepared

    def plot_compare(self):
        if not HAS_PYWT:
            messagebox.showerror("Missing PyWavelets", "Can cai PyWavelets:\npip install PyWavelets")
            return
        if not self.rows:
            messagebox.showwarning("No data", "Open a raw CSV file first.")
            return

        wavelets = self.selected_wavelets()
        if not wavelets:
            messagebox.showwarning("No wavelet", "Select at least one wavelet.")
            return

        try:
            level = int(self.level_var.get())
        except (TypeError, ValueError):
            level = 3
            self.level_var.set(level)

        time_s, raw, label = self.signal_values()
        if len(raw) < 8:
            messagebox.showwarning("Too few samples", "Selected signal has fewer than 8 valid samples.")
            return

        source = self.prepare_for_wavelet(raw)
        results: dict[str, np.ndarray] = {}
        errors: list[str] = []
        for wavelet in wavelets:
            try:
                filtered = wavelet_denoise(source, wavelet, level, self.threshold_var.get())
                if self.center_var.get():
                    filtered = filtered - np.nanmedian(filtered)
                results[wavelet] = filtered
            except Exception as exc:
                errors.append(f"{wavelet}: {exc}")

        if not results:
            messagebox.showerror("Wavelet Error", "\n".join(errors) or "No filter results.")
            return

        self.last_time_s = time_s
        self.last_raw = source
        self.last_results = results

        axis_count = len(results) + (1 if self.show_raw_var.get() else 0)
        self.figure.clear()
        axes = []
        for index in range(axis_count):
            sharex = axes[0] if axes else None
            axes.append(self.figure.add_subplot(axis_count, 1, index + 1, sharex=sharex))

        axis_index = 0
        if self.show_raw_var.get():
            axes[axis_index].plot(time_s, source, color="#4d4d4d", linewidth=0.8)
            axes[axis_index].set_ylabel("Raw")
            axes[axis_index].set_title(label)
            axes[axis_index].grid(True, alpha=0.25)
            axis_index += 1

        colors = ["#d62728", "#2ca02c", "#1f77b4", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#7f7f7f"]
        for offset, (wavelet, filtered) in enumerate(results.items()):
            ax = axes[axis_index + offset]
            ax.plot(time_s, filtered, color=colors[offset % len(colors)], linewidth=0.9)
            ax.set_ylabel(wavelet)
            ax.set_title(f"{self.signal_var.get()} - wavelet {wavelet}, level {level}, {self.threshold_var.get()}")
            ax.grid(True, alpha=0.25)

        axes[-1].set_xlabel("Time (s)")
        title = self.current_file.name if self.current_file else "Wavelet comparison"
        self.figure.suptitle(title)
        self.figure.tight_layout()
        self.canvas.draw_idle()

        status = f"Plotted {len(results)} wavelet filters on separate axes."
        if errors:
            status += " Skipped: " + " | ".join(errors[:3])
        self.status_var.set(status)

    def export_csv(self):
        if not self.last_results:
            messagebox.showwarning("No result", "Plot a comparison before exporting.")
            return

        base = self.current_file.stem if self.current_file else "wavelet_compare"
        default_name = f"{base}_{self.signal_var.get()}_wavelet_compare.csv"
        filename = filedialog.asksaveasfilename(
            title="Save wavelet comparison CSV",
            initialdir=str(COMPARE_DIR),
            initialfile=default_name,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return

        path = Path(filename)
        headers = ["time_s", "source_signal"] + [f"{name}_filtered" for name in self.last_results]
        try:
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for i, time_s in enumerate(self.last_time_s):
                    writer.writerow(
                        [f"{time_s:.6f}", f"{self.last_raw[i]:.6f}"]
                        + [f"{values[i]:.6f}" for values in self.last_results.values()]
                    )
        except Exception as exc:
            messagebox.showerror("Export Error", str(exc))
            self.status_var.set(f"ERROR: {exc}")
            return

        self.status_var.set(f"Exported {path}")


def main():
    if not HAS_PYWT:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Missing PyWavelets", "Can cai PyWavelets:\npip install PyWavelets")
        sys.exit(1)

    root = tk.Tk()
    WaveletComparatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
