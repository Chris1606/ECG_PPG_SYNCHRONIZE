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
RESULT_DIR = BASE_DIR / "bpm_sync_results"
RESULT_DIR.mkdir(parents=True, exist_ok=True)

ECG_SAMPLE_RATE_HZ = 1000.0
ECG_HIGHPASS_HZ = 0.5
ECG_LOWPASS_HZ = 45.0
ECG_MAINS_HZ = 50.0
ECG_MAINS_NOTCH_WIDTH_HZ = 2.0
DEFAULT_WAVELET = "db4"
DEFAULT_LEVEL = 3


@dataclass
class SyncRow:
    time_ms: int
    ecg_raw: float
    ppg_red_raw: float
    ppg_ir_raw: float


@dataclass
class PeakResult:
    time_s: np.ndarray
    value: np.ndarray
    indices: np.ndarray
    polarity: int
    threshold: float


@dataclass
class AnalysisResult:
    ecg_bpm_median: float
    ecg_bpm_count: float
    ppg_bpm_median: float
    ppg_bpm_count: float
    bpm_abs_diff: float
    bpm_percent_diff: float
    ecg_peaks: PeakResult
    ppg_peaks: PeakResult
    matched_ecg_times: np.ndarray
    matched_ppg_times: np.ndarray
    delays_ms: np.ndarray


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

            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5 and parts[1].lower() == "time_ms":
                continue
            if len(parts) >= 5:
                line = ",".join(parts[1:5])

            row = parse_sync_csv_line(line)
            if row is not None:
                rows.append(row)

    return normalize_sync_rows(rows)


def fft_band_clean(
    values: np.ndarray,
    sample_rate_hz: float,
    highpass_hz: float,
    lowpass_hz: float,
    notch_hz: float | None = None,
    notch_width_hz: float = 2.0,
) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < 8:
        return values.copy()

    baseline = float(np.nanmedian(values))
    centered = values - baseline
    spectrum = np.fft.rfft(centered)
    freqs = np.fft.rfftfreq(len(centered), d=1.0 / sample_rate_hz)

    keep = (freqs >= highpass_hz) & (freqs <= lowpass_hz)
    if notch_hz is not None:
        notch = np.abs(freqs - notch_hz) <= (notch_width_hz / 2.0)
        keep = keep & ~notch
    spectrum[~keep] = 0

    return np.fft.irfft(spectrum, n=len(centered))


def wavelet_denoise(values: np.ndarray, wavelet_name: str, level: int) -> np.ndarray:
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
        filtered_coeffs.append(pywt.threshold(detail, value=threshold, mode="soft"))
    return pywt.waverec(filtered_coeffs, wavelet_name, mode="symmetric")[:len(values)]


def robust_scale(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    mad = np.nanmedian(np.abs(values - np.nanmedian(values)))
    if mad > 0:
        return 1.4826 * mad
    std = np.nanstd(values)
    return float(std if std > 0 else 1.0)


def estimate_sample_rate(time_s: np.ndarray) -> float:
    if len(time_s) < 2:
        return 1.0
    diffs = np.diff(time_s)
    diffs = diffs[diffs > 0]
    if len(diffs) == 0:
        return 1.0
    return float(1.0 / np.median(diffs))


def auto_polarity(values: np.ndarray) -> int:
    values = np.asarray(values, dtype=float)
    pos = np.nanpercentile(values, 98) - np.nanmedian(values)
    neg = np.nanmedian(values) - np.nanpercentile(values, 2)
    return -1 if neg > pos else 1


def detect_peaks(
    time_s: np.ndarray,
    values: np.ndarray,
    min_distance_s: float,
    threshold_k: float,
    polarity: int = 0,
) -> PeakResult:
    if len(values) < 3:
        empty = np.array([], dtype=int)
        return PeakResult(time_s[empty], values[empty], empty, 1, np.nan)

    y = np.asarray(values, dtype=float)
    selected_polarity = auto_polarity(y) if polarity == 0 else polarity
    y_detect = y * selected_polarity

    center = np.nanmedian(y_detect)
    scale = robust_scale(y_detect)
    threshold = center + threshold_k * scale

    local_max = np.where((y_detect[1:-1] > y_detect[:-2]) & (y_detect[1:-1] >= y_detect[2:]))[0] + 1
    candidates = local_max[y_detect[local_max] >= threshold]

    if len(candidates) == 0:
        threshold = np.nanpercentile(y_detect, 80)
        candidates = local_max[y_detect[local_max] >= threshold]

    fs = estimate_sample_rate(time_s)
    min_distance_samples = max(1, int(round(min_distance_s * fs)))

    # Keep the tallest candidate first, then suppress neighbors inside the refractory window.
    ordered = candidates[np.argsort(y_detect[candidates])[::-1]]
    kept: list[int] = []
    blocked = np.zeros(len(y_detect), dtype=bool)
    for idx in ordered:
        if blocked[idx]:
            continue
        kept.append(int(idx))
        lo = max(0, idx - min_distance_samples)
        hi = min(len(blocked), idx + min_distance_samples + 1)
        blocked[lo:hi] = True

    indices = np.array(sorted(kept), dtype=int)
    return PeakResult(time_s[indices], y[indices], indices, selected_polarity, float(threshold))


def bpm_from_peaks(peak_times_s: np.ndarray) -> tuple[float, float]:
    if len(peak_times_s) < 2:
        return np.nan, np.nan

    intervals = np.diff(peak_times_s)
    intervals = intervals[(intervals > 0.25) & (intervals < 2.5)]
    bpm_median = 60.0 / np.median(intervals) if len(intervals) else np.nan

    duration = peak_times_s[-1] - peak_times_s[0]
    bpm_count = ((len(peak_times_s) - 1) * 60.0 / duration) if duration > 0 else np.nan
    return float(bpm_median), float(bpm_count)


def match_ecg_to_ppg(
    ecg_times_s: np.ndarray,
    ppg_times_s: np.ndarray,
    min_delay_s: float,
    max_delay_s: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    matched_ecg = []
    matched_ppg = []
    delays_ms = []
    ppg_index = 0

    for ecg_t in ecg_times_s:
        while ppg_index < len(ppg_times_s) and ppg_times_s[ppg_index] < ecg_t + min_delay_s:
            ppg_index += 1
        if ppg_index >= len(ppg_times_s):
            break
        ppg_t = ppg_times_s[ppg_index]
        delay_s = ppg_t - ecg_t
        if min_delay_s <= delay_s <= max_delay_s:
            matched_ecg.append(ecg_t)
            matched_ppg.append(ppg_t)
            delays_ms.append(delay_s * 1000.0)
            ppg_index += 1

    return (
        np.array(matched_ecg, dtype=float),
        np.array(matched_ppg, dtype=float),
        np.array(delays_ms, dtype=float),
    )


def format_value(value: float, unit: str = "") -> str:
    if not np.isfinite(value):
        return "N/A"
    return f"{value:.2f}{unit}"


def summarize_delays(delays_ms: np.ndarray) -> dict[str, float]:
    if len(delays_ms) == 0:
        return {
            "mean": np.nan,
            "median": np.nan,
            "std": np.nan,
            "min": np.nan,
            "max": np.nan,
        }
    return {
        "mean": float(np.mean(delays_ms)),
        "median": float(np.median(delays_ms)),
        "std": float(np.std(delays_ms, ddof=1)) if len(delays_ms) > 1 else 0.0,
        "min": float(np.min(delays_ms)),
        "max": float(np.max(delays_ms)),
    }


class BPMSyncAnalyzerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("BPM and ECG-PPG Sync Analyzer")
        self.root.geometry("1220x820")
        self.root.minsize(980, 680)

        self.rows: list[SyncRow] = []
        self.current_file: Path | None = None
        self.result: AnalysisResult | None = None
        self.ecg_time_s = np.array([], dtype=float)
        self.ecg_filtered = np.array([], dtype=float)
        self.ppg_time_s = np.array([], dtype=float)
        self.ppg_filtered = np.array([], dtype=float)

        self.wavelet_var = tk.StringVar(value=DEFAULT_WAVELET)
        self.level_var = tk.IntVar(value=DEFAULT_LEVEL)
        self.ppg_signal_var = tk.StringVar(value="PPG_IR")
        self.ecg_threshold_var = tk.DoubleVar(value=2.8)
        self.ppg_threshold_var = tk.DoubleVar(value=0.8)
        self.min_delay_var = tk.DoubleVar(value=80.0)
        self.max_delay_var = tk.DoubleVar(value=800.0)
        self.status_var = tk.StringVar(value="Open a raw CSV file to calculate ECG BPM, PPG BPM, and ECG-to-PPG delay.")

        self.build_ui()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="Open Raw CSV", command=self.open_file).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(top, text="Analyze BPM", command=self.analyze).grid(row=0, column=1, padx=4)
        ttk.Button(top, text="Export Report", command=self.export_report).grid(row=0, column=2, padx=4)

        ttk.Label(top, text="PPG").grid(row=0, column=3, padx=(18, 4), sticky="e")
        ttk.Combobox(top, textvariable=self.ppg_signal_var, values=["PPG_IR", "PPG_RED"], width=9, state="readonly").grid(row=0, column=4, padx=4)

        ttk.Label(top, text="Wavelet").grid(row=0, column=5, padx=(18, 4), sticky="e")
        wavelets = pywt.wavelist(kind="discrete") if HAS_PYWT else [DEFAULT_WAVELET]
        ttk.Combobox(top, textvariable=self.wavelet_var, values=wavelets, width=10, state="readonly").grid(row=0, column=6, padx=4)

        ttk.Label(top, text="Level").grid(row=0, column=7, padx=(18, 4), sticky="e")
        ttk.Spinbox(top, from_=1, to=8, textvariable=self.level_var, width=5).grid(row=0, column=8, padx=4)

        params = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        params.pack(fill="x")
        ttk.Label(params, text="ECG peak k").pack(side="left")
        ttk.Spinbox(params, from_=0.5, to=8.0, increment=0.1, textvariable=self.ecg_threshold_var, width=6).pack(side="left", padx=(4, 18))
        ttk.Label(params, text="PPG peak k").pack(side="left")
        ttk.Spinbox(params, from_=0.2, to=8.0, increment=0.1, textvariable=self.ppg_threshold_var, width=6).pack(side="left", padx=(4, 18))
        ttk.Label(params, text="ECG->PPG min delay ms").pack(side="left")
        ttk.Spinbox(params, from_=0, to=1000, increment=10, textvariable=self.min_delay_var, width=7).pack(side="left", padx=(4, 18))
        ttk.Label(params, text="max delay ms").pack(side="left")
        ttk.Spinbox(params, from_=100, to=2000, increment=10, textvariable=self.max_delay_var, width=7).pack(side="left", padx=(4, 18))

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        plot_frame = ttk.Frame(body)
        body.add(plot_frame, weight=4)
        self.figure = Figure(figsize=(9, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, plot_frame)

        side = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(side, weight=1)
        ttk.Label(side, text="Metrics").pack(anchor="w")
        self.metrics = tk.Text(side, height=28, wrap="word")
        self.metrics.pack(fill="both", expand=True)

        ttk.Label(self.root, textvariable=self.status_var, padding=(10, 0)).pack(fill="x")

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
        self.analyze()

    def extract_signal(self, signal_name: str) -> tuple[np.ndarray, np.ndarray]:
        rows = normalize_sync_rows(self.rows)
        time_ms = np.array([row.time_ms for row in rows], dtype=float)
        if signal_name == "ECG":
            values = np.array([row.ecg_raw for row in rows], dtype=float)
        elif signal_name == "PPG_RED":
            values = np.array([row.ppg_red_raw for row in rows], dtype=float)
        else:
            values = np.array([row.ppg_ir_raw for row in rows], dtype=float)
        mask = np.isfinite(values)
        return time_ms[mask] / 1000.0, values[mask]

    def analyze(self):
        if not HAS_PYWT:
            messagebox.showerror("Missing PyWavelets", "Can cai PyWavelets:\npip install PyWavelets")
            return
        if not self.rows:
            messagebox.showwarning("No data", "Open a raw CSV file first.")
            return

        try:
            level = int(self.level_var.get())
            min_delay_s = float(self.min_delay_var.get()) / 1000.0
            max_delay_s = float(self.max_delay_var.get()) / 1000.0
            ecg_k = float(self.ecg_threshold_var.get())
            ppg_k = float(self.ppg_threshold_var.get())
        except (TypeError, ValueError):
            messagebox.showerror("Parameter Error", "Peak and delay parameters must be numeric.")
            return

        if min_delay_s >= max_delay_s:
            messagebox.showerror("Parameter Error", "Min delay must be smaller than max delay.")
            return

        try:
            ecg_time, ecg_raw = self.extract_signal("ECG")
            ppg_time, ppg_raw = self.extract_signal(self.ppg_signal_var.get())
            if len(ecg_raw) < 8 or len(ppg_raw) < 8:
                raise ValueError("Need both ECG and PPG samples in the raw file.")

            ecg_clean = fft_band_clean(
                ecg_raw,
                sample_rate_hz=ECG_SAMPLE_RATE_HZ,
                highpass_hz=ECG_HIGHPASS_HZ,
                lowpass_hz=ECG_LOWPASS_HZ,
                notch_hz=ECG_MAINS_HZ,
                notch_width_hz=ECG_MAINS_NOTCH_WIDTH_HZ,
            )
            ecg_filtered = wavelet_denoise(ecg_clean, self.wavelet_var.get(), level)
            ecg_filtered = ecg_filtered - np.nanmedian(ecg_filtered)

            ppg_fs = estimate_sample_rate(ppg_time)
            ppg_clean = fft_band_clean(ppg_raw, sample_rate_hz=ppg_fs, highpass_hz=0.4, lowpass_hz=8.0)
            ppg_filtered = wavelet_denoise(ppg_clean, self.wavelet_var.get(), level)
            ppg_filtered = ppg_filtered - np.nanmedian(ppg_filtered)

            ecg_peaks = detect_peaks(ecg_time, ecg_filtered, min_distance_s=0.30, threshold_k=ecg_k, polarity=0)
            ppg_peaks = detect_peaks(ppg_time, ppg_filtered, min_distance_s=0.35, threshold_k=ppg_k, polarity=0)

            ecg_bpm_median, ecg_bpm_count = bpm_from_peaks(ecg_peaks.time_s)
            ppg_bpm_median, ppg_bpm_count = bpm_from_peaks(ppg_peaks.time_s)
            bpm_abs_diff = abs(ecg_bpm_median - ppg_bpm_median) if np.isfinite(ecg_bpm_median) and np.isfinite(ppg_bpm_median) else np.nan
            bpm_percent_diff = (bpm_abs_diff / ecg_bpm_median * 100.0) if np.isfinite(bpm_abs_diff) and ecg_bpm_median > 0 else np.nan

            matched_ecg, matched_ppg, delays_ms = match_ecg_to_ppg(ecg_peaks.time_s, ppg_peaks.time_s, min_delay_s, max_delay_s)

            self.ecg_time_s = ecg_time
            self.ecg_filtered = ecg_filtered
            self.ppg_time_s = ppg_time
            self.ppg_filtered = ppg_filtered
            self.result = AnalysisResult(
                ecg_bpm_median=ecg_bpm_median,
                ecg_bpm_count=ecg_bpm_count,
                ppg_bpm_median=ppg_bpm_median,
                ppg_bpm_count=ppg_bpm_count,
                bpm_abs_diff=bpm_abs_diff,
                bpm_percent_diff=bpm_percent_diff,
                ecg_peaks=ecg_peaks,
                ppg_peaks=ppg_peaks,
                matched_ecg_times=matched_ecg,
                matched_ppg_times=matched_ppg,
                delays_ms=delays_ms,
            )
        except Exception as exc:
            messagebox.showerror("Analysis Error", str(exc))
            self.status_var.set(f"ERROR: {exc}")
            return

        self.update_metrics()
        self.plot_result()
        self.status_var.set("Analysis complete. Adjust peak thresholds if markers miss or over-detect peaks.")

    def update_metrics(self):
        if self.result is None:
            return

        result = self.result
        delays = summarize_delays(result.delays_ms)
        file_name = self.current_file.name if self.current_file else "N/A"

        text = [
            f"File: {file_name}",
            f"Wavelet: {self.wavelet_var.get()} level {self.level_var.get()}",
            "",
            "BPM from ECG R-peaks",
            f"- Peaks: {len(result.ecg_peaks.time_s)}",
            f"- BPM median RR: {format_value(result.ecg_bpm_median, ' bpm')}",
            f"- BPM count-based: {format_value(result.ecg_bpm_count, ' bpm')}",
            "",
            f"BPM from {self.ppg_signal_var.get()} peaks",
            f"- Peaks: {len(result.ppg_peaks.time_s)}",
            f"- BPM median PP: {format_value(result.ppg_bpm_median, ' bpm')}",
            f"- BPM count-based: {format_value(result.ppg_bpm_count, ' bpm')}",
            "",
            "ECG vs PPG BPM difference",
            f"- Absolute diff: {format_value(result.bpm_abs_diff, ' bpm')}",
            f"- Percent diff: {format_value(result.bpm_percent_diff, '%')}",
            "",
            "ECG R-peak -> PPG peak delay",
            f"- Matched beats: {len(result.delays_ms)}",
            f"- Mean delay: {format_value(delays['mean'], ' ms')}",
            f"- Median delay: {format_value(delays['median'], ' ms')}",
            f"- Delay std/jitter: {format_value(delays['std'], ' ms')}",
            f"- Min delay: {format_value(delays['min'], ' ms')}",
            f"- Max delay: {format_value(delays['max'], ' ms')}",
            "",
            "Interpretation note",
            "The ECG->PPG delay is not pure device sync error.",
            "It includes physiological pulse transit time, sensor response,",
            "filter delay, and peak detection uncertainty.",
            "For sync stability, use delay std/jitter and unmatched beat count.",
        ]

        self.metrics.delete("1.0", "end")
        self.metrics.insert("end", "\n".join(text))

    def plot_result(self):
        if self.result is None:
            return

        result = self.result
        self.figure.clear()
        ax_ecg = self.figure.add_subplot(3, 1, 1)
        ax_ppg = self.figure.add_subplot(3, 1, 2, sharex=ax_ecg)
        ax_delay = self.figure.add_subplot(3, 1, 3)

        ax_ecg.plot(self.ecg_time_s, self.ecg_filtered, color="#d62728", linewidth=0.8)
        ax_ecg.scatter(result.ecg_peaks.time_s, result.ecg_peaks.value, s=24, color="#000000", marker="x", label="R-peaks")
        ax_ecg.set_title("Filtered ECG with detected R-peaks")
        ax_ecg.set_ylabel("ECG")
        ax_ecg.grid(True, alpha=0.25)
        ax_ecg.legend(loc="upper right")

        ax_ppg.plot(self.ppg_time_s, self.ppg_filtered, color="#2ca02c", linewidth=0.9)
        ax_ppg.scatter(result.ppg_peaks.time_s, result.ppg_peaks.value, s=24, color="#000000", marker="x", label="PPG peaks")
        for ecg_t, ppg_t in zip(result.matched_ecg_times, result.matched_ppg_times):
            ax_ppg.axvspan(ecg_t, ppg_t, color="#f0ad4e", alpha=0.12)
        ax_ppg.set_title(f"Filtered {self.ppg_signal_var.get()} with detected pulse peaks")
        ax_ppg.set_ylabel("PPG")
        ax_ppg.grid(True, alpha=0.25)
        ax_ppg.legend(loc="upper right")

        if len(result.delays_ms):
            beat_index = np.arange(1, len(result.delays_ms) + 1)
            ax_delay.plot(beat_index, result.delays_ms, color="#1f77b4", marker="o", linewidth=0.9)
            ax_delay.axhline(np.mean(result.delays_ms), color="#d62728", linestyle="--", linewidth=0.9, label="mean")
            ax_delay.legend(loc="upper right")
        ax_delay.set_title("ECG R-peak to PPG peak delay per matched beat")
        ax_delay.set_xlabel("Matched beat index")
        ax_delay.set_ylabel("Delay (ms)")
        ax_delay.grid(True, alpha=0.25)

        title = self.current_file.name if self.current_file else "BPM and sync analysis"
        self.figure.suptitle(title)
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def export_report(self):
        if self.result is None:
            messagebox.showwarning("No result", "Run Analyze BPM before exporting.")
            return

        base = self.current_file.stem if self.current_file else "bpm_sync"
        default_name = f"{base}_bpm_sync_report.csv"
        filename = filedialog.asksaveasfilename(
            title="Save BPM/sync report CSV",
            initialdir=str(RESULT_DIR),
            initialfile=default_name,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return

        path = Path(filename)
        result = self.result
        delays = summarize_delays(result.delays_ms)

        try:
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["metric", "value"])
                writer.writerow(["source_file", self.current_file.name if self.current_file else ""])
                writer.writerow(["wavelet", self.wavelet_var.get()])
                writer.writerow(["wavelet_level", self.level_var.get()])
                writer.writerow(["ppg_signal", self.ppg_signal_var.get()])
                writer.writerow(["ecg_peak_count", len(result.ecg_peaks.time_s)])
                writer.writerow(["ppg_peak_count", len(result.ppg_peaks.time_s)])
                writer.writerow(["ecg_bpm_median_rr", result.ecg_bpm_median])
                writer.writerow(["ecg_bpm_count_based", result.ecg_bpm_count])
                writer.writerow(["ppg_bpm_median_pp", result.ppg_bpm_median])
                writer.writerow(["ppg_bpm_count_based", result.ppg_bpm_count])
                writer.writerow(["bpm_abs_diff", result.bpm_abs_diff])
                writer.writerow(["bpm_percent_diff", result.bpm_percent_diff])
                writer.writerow(["matched_beats", len(result.delays_ms)])
                writer.writerow(["delay_mean_ms", delays["mean"]])
                writer.writerow(["delay_median_ms", delays["median"]])
                writer.writerow(["delay_std_jitter_ms", delays["std"]])
                writer.writerow(["delay_min_ms", delays["min"]])
                writer.writerow(["delay_max_ms", delays["max"]])
                writer.writerow([])
                writer.writerow(["beat_index", "ecg_peak_s", "ppg_peak_s", "delay_ms"])
                for index, (ecg_t, ppg_t, delay_ms) in enumerate(
                    zip(result.matched_ecg_times, result.matched_ppg_times, result.delays_ms),
                    start=1,
                ):
                    writer.writerow([index, f"{ecg_t:.6f}", f"{ppg_t:.6f}", f"{delay_ms:.3f}"])
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
    BPMSyncAnalyzerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
