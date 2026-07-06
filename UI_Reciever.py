import csv
import re
import sys
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    messagebox.showerror("Thieu thu vien", "Can cai pyserial:\npip install pyserial")
    sys.exit(1)

try:
    import pywt
    HAS_PYWT = True
except ImportError:
    HAS_PYWT = False

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure


BAUD_RATE = 115200
WAVELET_NAME = "db4"
WAVELET_LEVEL = 3
ECG_SAMPLE_RATE_HZ = 1000.0
ECG_HIGHPASS_HZ = 0.5
ECG_LOWPASS_HZ = 45.0
ECG_MAINS_HZ = 50.0
ECG_MAINS_NOTCH_WIDTH_HZ = 2.0
ECG_CLIP_LOW_THRESHOLD = 20.0
ECG_CLIP_HIGH_THRESHOLD = 4075.0
BASE_DIR = Path("data_csv") / "SYNC"
RAW_DIR = BASE_DIR / "raw"
FILTERED_DIR = BASE_DIR / "filtered"
RAW_DIR.mkdir(parents=True, exist_ok=True)
FILTERED_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", text)
    return text.strip("_") or "unknown"


def wavelet_denoise(values: np.ndarray, wavelet_name: str = WAVELET_NAME, level: int = WAVELET_LEVEL) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < 8:
        return values.copy()
    if not HAS_PYWT:
        return np.full(len(values), np.nan)

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


def ecg_denoise(values: np.ndarray) -> np.ndarray:
    cleaned = fft_ecg_band_clean(values)
    filtered = wavelet_denoise(cleaned, wavelet_name=WAVELET_NAME, level=WAVELET_LEVEL)
    return filtered - np.nanmedian(filtered)


def summarize_signal_quality(rows: list["SyncRow"], duration_s: int) -> str:
    ecg = np.array([row.ecg_raw for row in rows if np.isfinite(row.ecg_raw)], dtype=float)
    ppg = np.array([row.ppg_ir_raw for row in rows if np.isfinite(row.ppg_ir_raw)], dtype=float)
    notes: list[str] = []

    if len(ecg):
        clip_low = int(np.count_nonzero(ecg <= ECG_CLIP_LOW_THRESHOLD))
        clip_high = int(np.count_nonzero(ecg >= ECG_CLIP_HIGH_THRESHOLD))
        if clip_low or clip_high:
            notes.append(f"ECG clipping low={clip_low}, high={clip_high}")
        notes.append(f"ECG n={len(ecg)}")

    if len(ppg):
        rate = len(ppg) / max(duration_s, 1)
        notes.append(f"PPG n={len(ppg)} (~{rate:.1f}Hz)")

    return " | ".join(notes)


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

            # Saved raw CSV has person_name,time_ms,...; UART capture has time_ms,...
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5 and parts[1].lower() == "time_ms":
                continue
            if len(parts) >= 5:
                line = ",".join(parts[1:5])

            row = parse_sync_csv_line(line)
            if row is not None:
                rows.append(row)

    return normalize_sync_rows(rows)


class SyncUARTMonitor:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ECG + PPG Sync UART Logger")
        self.root.geometry("1180x760")
        self.root.minsize(960, 640)

        self.serial_obj = None
        self.worker = None
        self.stop_event = threading.Event()
        self.latest_raw_file = None
        self.latest_filtered_file = None

        self.port_var = tk.StringVar()
        self.person_var = tk.StringVar(value="unknown")
        self.duration_var = tk.StringVar(value="10")
        self.mode_var = tk.StringVar(value="BOTH")
        self.status_var = tk.StringVar(value="Ready")

        self.build_ui()
        self.refresh_ports()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="COM").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=18, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=6, sticky="w")
        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=4)

        ttk.Label(top, text="Nguoi do").grid(row=0, column=3, sticky="w", padx=(18, 0))
        ttk.Entry(top, textvariable=self.person_var, width=18).grid(row=0, column=4, padx=6)

        ttk.Label(top, text="Mode").grid(row=0, column=5, sticky="w", padx=(18, 0))
        ttk.Combobox(top, textvariable=self.mode_var, values=["BOTH", "ECG", "PPG"], width=8, state="readonly").grid(row=0, column=6, padx=6)

        ttk.Label(top, text="Giay").grid(row=0, column=7, sticky="w", padx=(18, 0))
        ttk.Entry(top, textvariable=self.duration_var, width=8).grid(row=0, column=8, padx=6)

        ttk.Button(top, text="Start", command=self.start_measurement).grid(row=0, column=9, padx=(18, 4))
        ttk.Button(top, text="Stop", command=self.stop_measurement).grid(row=0, column=10, padx=4)
        ttk.Button(top, text="Open CSV", command=self.open_csv_file).grid(row=0, column=11, padx=4)

        self.progress = ttk.Progressbar(top, mode="determinate", length=180)
        self.progress.grid(row=0, column=12, padx=(18, 0), sticky="ew")
        top.columnconfigure(12, weight=1)

        ttk.Label(self.root, textvariable=self.status_var, padding=(10, 0)).pack(fill="x")

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=10)

        plot_frame = ttk.Frame(body)
        body.add(plot_frame, weight=4)

        self.figure = Figure(figsize=(8, 5), dpi=100)
        self.ax_ecg = self.figure.add_subplot(211)
        self.ax_ppg = self.figure.add_subplot(212, sharex=self.ax_ecg)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, plot_frame)

        log_frame = ttk.Frame(body)
        body.add(log_frame, weight=1)
        ttk.Label(log_frame, text="UART log").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=10, wrap="none")
        self.log_text.pack(fill="both", expand=True)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def set_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(text))

    def log(self, text: str):
        def append():
            self.log_text.insert("end", text + "\n")
            self.log_text.see("end")
        self.root.after(0, append)

    def start_measurement(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Dang do", "Dang co phien do khac.")
            return
        if not self.port_var.get():
            messagebox.showerror("COM", "Chua chon cong COM.")
            return
        try:
            duration_s = int(self.duration_var.get())
            if duration_s <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Thoi gian", "Thoi gian do phai la so nguyen duong.")
            return

        self.stop_event.clear()
        self.progress["maximum"] = duration_s
        self.progress["value"] = 0
        self.worker = threading.Thread(target=self.measure_worker, args=(duration_s,), daemon=True)
        self.worker.start()
        self.update_progress(duration_s, time.time())

    def stop_measurement(self):
        self.stop_event.set()
        if self.serial_obj and self.serial_obj.is_open:
            try:
                self.serial_obj.write(b"STOP\n")
            except Exception:
                pass
        self.set_status("Dang dung...")

    def update_progress(self, duration_s: int, start_time: float):
        if not (self.worker and self.worker.is_alive()):
            return
        elapsed = min(duration_s, time.time() - start_time)
        self.progress["value"] = elapsed
        self.root.after(200, lambda: self.update_progress(duration_s, start_time))

    def open_csv_file(self):
        initial_dir = RAW_DIR if RAW_DIR.exists() else Path.cwd()
        filename = filedialog.askopenfilename(
            title="Chon file CSV raw/capture",
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
                raise ValueError("Khong tim thay dong time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw hop le.")
            self.plot_rows(rows, path.name)
            self.set_status(f"Da mo file: {path}")
            self.log(f"[OPEN] {path}")
        except Exception as exc:
            messagebox.showerror("Open CSV Error", str(exc))
            self.set_status(f"ERROR: {exc}")

    def measure_worker(self, duration_s: int):
        mode = self.mode_var.get().upper()
        person = self.person_var.get().strip() or "unknown"
        port = self.port_var.get()

        try:
            self.set_status(f"Mo {port}...")
            with serial.Serial(port, BAUD_RATE, timeout=1.0) as ser:
                self.serial_obj = ser
                time.sleep(0.3)
                ser.reset_input_buffer()

                board_mode = "BOTH" if mode == "BOTH" else ("ECG" if mode == "ECG" else "PPG")
                ser.write((board_mode + "\n").encode("utf-8"))
                time.sleep(0.1)
                ser.write((f"START {duration_s}\n").encode("utf-8"))
                self.log(f"> {board_mode}")
                self.log(f"> START {duration_s}")
                self.set_status("Dang do tren ESP32, du lieu se gui sau khi do xong...")

                rows = self.read_sync_block(ser, timeout_s=duration_s + 25)
                if self.stop_event.is_set():
                    self.set_status("Da dung.")
                    return
                if not rows:
                    raise RuntimeError("Khong nhan duoc du lieu CSV tu ESP32.")

                raw_file = self.save_raw_csv(rows, person, mode)
                filtered_file = self.save_filtered_csv(rows, person, mode)
                quality = summarize_signal_quality(rows, duration_s)
                self.latest_raw_file = raw_file
                self.latest_filtered_file = filtered_file
                self.root.after(0, lambda: self.plot_rows(rows, raw_file.name))
                self.set_status(f"Da luu raw: {raw_file.name} | filtered: {filtered_file.name} | {quality}")
        except Exception as exc:
            self.set_status(f"ERROR: {exc}")
            self.root.after(0, lambda: messagebox.showerror("UART Error", str(exc)))
        finally:
            self.serial_obj = None
            self.root.after(0, lambda: self.progress.configure(value=0))

    def read_sync_block(self, ser: serial.Serial, timeout_s: float) -> list[SyncRow]:
        deadline = time.time() + timeout_s
        in_block = False
        rows: list[SyncRow] = []

        while time.time() < deadline and not self.stop_event.is_set():
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            self.log(line)

            upper = line.upper()
            if upper.startswith("BEGIN_SYNC_CSV"):
                in_block = True
                rows.clear()
                continue
            if upper.startswith("END_SYNC_CSV"):
                return normalize_sync_rows(rows)
            if not in_block:
                continue
            if upper.startswith("TIME_MS"):
                continue

            row = parse_sync_csv_line(line)
            if row is not None:
                rows.append(row)

        raise TimeoutError("Het thoi gian cho block BEGIN_SYNC_CSV/END_SYNC_CSV.")

    def make_file_base(self, person: str, mode: str) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{safe_filename(person)}_{mode}_sync_{ts}"

    def save_raw_csv(self, rows: list[SyncRow], person: str, mode: str) -> Path:
        rows = normalize_sync_rows(rows)
        path = RAW_DIR / f"{self.make_file_base(person, mode)}_raw.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["person_name", "time_ms", "ecg_raw", "ppg_red_raw", "ppg_ir_raw"])
            for row in rows:
                writer.writerow([
                    person,
                    row.time_ms,
                    "" if np.isnan(row.ecg_raw) else f"{row.ecg_raw:.0f}",
                    "" if np.isnan(row.ppg_red_raw) else f"{row.ppg_red_raw:.0f}",
                    "" if np.isnan(row.ppg_ir_raw) else f"{row.ppg_ir_raw:.0f}",
                ])
        return path

    def save_filtered_csv(self, rows: list[SyncRow], person: str, mode: str) -> Path:
        rows = normalize_sync_rows(rows)
        path = FILTERED_DIR / f"{self.make_file_base(person, mode)}_wavelet.csv"
        ecg_filtered, ppg_filtered = self.compute_filtered(rows)

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "person_name", "time_ms",
                "ecg_raw", f"ecg_centered_bandpass_wavelet_{WAVELET_NAME}_level{WAVELET_LEVEL}",
                "ppg_ir_raw", f"ppg_ir_wavelet_{WAVELET_NAME}_level{WAVELET_LEVEL}",
                "ppg_red_raw",
            ])
            for i, row in enumerate(rows):
                writer.writerow([
                    person,
                    row.time_ms,
                    "" if np.isnan(row.ecg_raw) else f"{row.ecg_raw:.0f}",
                    "" if np.isnan(ecg_filtered[i]) else f"{ecg_filtered[i]:.6f}",
                    "" if np.isnan(row.ppg_ir_raw) else f"{row.ppg_ir_raw:.0f}",
                    "" if np.isnan(ppg_filtered[i]) else f"{ppg_filtered[i]:.6f}",
                    "" if np.isnan(row.ppg_red_raw) else f"{row.ppg_red_raw:.0f}",
                ])
        return path

    def compute_filtered(self, rows: list[SyncRow]) -> tuple[np.ndarray, np.ndarray]:
        rows = normalize_sync_rows(rows)
        ecg_filtered = np.full(len(rows), np.nan)
        ppg_filtered = np.full(len(rows), np.nan)

        ecg_idx = np.array([i for i, row in enumerate(rows) if not np.isnan(row.ecg_raw)], dtype=int)
        if len(ecg_idx):
            ecg_values = np.array([rows[i].ecg_raw for i in ecg_idx], dtype=float)
            ecg_filtered[ecg_idx] = ecg_denoise(ecg_values)

        ppg_idx = np.array([i for i, row in enumerate(rows) if not np.isnan(row.ppg_ir_raw)], dtype=int)
        if len(ppg_idx):
            ppg_values = np.array([rows[i].ppg_ir_raw for i in ppg_idx], dtype=float)
            ppg_filtered[ppg_idx] = wavelet_denoise(ppg_values)

        return ecg_filtered, ppg_filtered

    def plot_rows(self, rows: list[SyncRow], title: str):
        rows = normalize_sync_rows(rows)
        ecg_filtered, ppg_filtered = self.compute_filtered(rows)
        t = np.array([row.time_ms / 1000.0 for row in rows], dtype=float)
        ecg = np.array([row.ecg_raw for row in rows], dtype=float)
        ppg = np.array([row.ppg_ir_raw for row in rows], dtype=float)
        ecg_mask = np.isfinite(ecg)
        if ecg_mask.any():
            ecg_baseline = np.nanmedian(ecg[ecg_mask])
            ecg = ecg - ecg_baseline
            filtered_mask = np.isfinite(ecg_filtered)
            if filtered_mask.any():
                ecg_filtered = ecg_filtered - np.nanmedian(ecg_filtered[filtered_mask])

        self.ax_ecg.clear()
        self.ax_ppg.clear()

        if ecg_mask.any():
            self.ax_ecg.plot(t[ecg_mask], ecg[ecg_mask], color="#999999", linewidth=0.8, alpha=0.65, label="ECG raw")
            self.ax_ecg.plot(t[ecg_mask], ecg_filtered[ecg_mask], color="#d62728", linewidth=1.1, label="ECG filtered")
        self.ax_ecg.set_ylabel("ECG centered")
        self.ax_ecg.grid(False)
        self.ax_ecg.legend(loc="upper right")

        ppg_mask = np.isfinite(ppg)
        if ppg_mask.any():
            self.ax_ppg.plot(t[ppg_mask], ppg[ppg_mask], color="#999999", linewidth=0.8, alpha=0.65, label="PPG IR raw")
            self.ax_ppg.plot(t[ppg_mask], ppg_filtered[ppg_mask], color="#2ca02c", linewidth=1.1, label="PPG IR wavelet")
        self.ax_ppg.set_xlabel("Time (s)")
        self.ax_ppg.set_ylabel("PPG IR raw")
        self.ax_ppg.grid(True, alpha=0.25)
        self.ax_ppg.legend(loc="upper right")

        self.figure.suptitle(title)
        self.figure.tight_layout()
        self.canvas.draw_idle()


def main():
    root = tk.Tk()
    SyncUARTMonitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
