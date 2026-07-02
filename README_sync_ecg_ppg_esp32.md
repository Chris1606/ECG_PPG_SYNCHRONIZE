# Đồng bộ tín hiệu ECG AD8232 và PPG MAX30102 trên ESP32

## 1. Mục tiêu

Hệ thống dùng ESP32 để thu đồng thời hai tín hiệu sinh học:

- ECG từ module AD8232
- PPG từ cảm biến MAX30102

Hai tín hiệu được đồng bộ bằng cùng một bộ đếm thời gian nội bộ trên ESP32. Sau khi đo xong, dữ liệu thô được gửi về máy tính qua UART và lưu thành file CSV để phục vụ xử lý tín hiệu sau này, ví dụ lọc nhiễu bằng Wavelet.

---

## 2. Nguyên lý đồng bộ

Hệ thống sử dụng một timer chung làm mốc thời gian chính.

Timer được cấu hình với tần số 1000 Hz, tương ứng mỗi tick là 1 ms.

```text
1 tick = 1 ms
```

Timer này dùng để:

- Tạo timestamp chung cho toàn bộ hệ thống
- Điều khiển quá trình lấy mẫu ECG
- Gắn mốc thời gian cho dữ liệu PPG đọc từ FIFO của MAX30102

---

## 3. Luồng hoạt động tổng quát

```text
Máy tính gửi lệnh UART
        ↓
ESP32 nhận lệnh bắt đầu đo
        ↓
Người dùng cấu hình thời gian đo
        ↓
ESP32 bắt đầu timer đồng bộ
        ↓
Thu ECG bằng ADC + DMA
        ↓
Thu PPG bằng MAX30102 FIFO
        ↓
Gắn timestamp cho từng mẫu
        ↓
Kết thúc sau thời gian đo đã cấu hình
        ↓
Gửi dữ liệu thô về máy tính qua UART
        ↓
Máy tính lưu dữ liệu thành file CSV
        ↓
Xử lý tín hiệu: lọc Wavelet, lọc thông thấp, tính nhịp tim, ...
```

---

## 4. Thu tín hiệu ECG từ AD8232

AD8232 xuất tín hiệu analog, vì vậy ESP32 đọc tín hiệu này thông qua ADC.

ECG được lấy mẫu với tần số 1000 Hz.

```text
Timer 1000 Hz
    ↓
Trigger ADC
    ↓
DMA ghi dữ liệu vào buffer RAM
```

Mỗi mẫu ECG được gắn timestamp theo đơn vị ms:

```text
ecg_time_ms = sample_index
```

Ví dụ:

```text
Mẫu ECG thứ 0  → 0 ms
Mẫu ECG thứ 1  → 1 ms
Mẫu ECG thứ 2  → 2 ms
...
```

---

## 5. Thu tín hiệu PPG từ MAX30102

MAX30102 là cảm biến số, dữ liệu PPG được lưu trong FIFO nội bộ của cảm biến.

MAX30102 có thể được cấu hình sample rate riêng, ví dụ:

```text
PPG sample rate = 100 Hz hoặc 200 Hz
```

Không cần ép PPG lấy mẫu ở 1000 Hz giống ECG. Điều quan trọng là mỗi mẫu PPG phải được gắn timestamp theo cùng hệ thời gian với ECG.

Khi FIFO của MAX30102 có dữ liệu, ESP32 đọc dữ liệu qua I2C và lưu vào buffer RAM.

Ví dụ với PPG sample rate = 100 Hz:

```text
T_ppg = 10 ms
```

Nếu tại thời điểm `now_ms`, ESP32 đọc được 16 mẫu PPG từ FIFO, timestamp của từng mẫu được tính như sau:

```text
ppg[15] → now_ms
ppg[14] → now_ms - 10
ppg[13] → now_ms - 20
...
ppg[0]  → now_ms - 150
```

Công thức tổng quát:

```c
timestamp_ms = now_ms - (num_samples - 1 - i) * ppg_period_ms;
```

Trong đó:

```c
ppg_period_ms = 1000 / ppg_sample_rate;
```

---

## 6. Cấu trúc dữ liệu sau khi đo

Mỗi mẫu dữ liệu nên được lưu kèm timestamp.

Ví dụ định dạng dữ liệu nội bộ:

```c
typedef struct {
    uint32_t time_ms;
    uint16_t ecg_raw;
} ecg_sample_t;

typedef struct {
    uint32_t time_ms;
    uint32_t red_raw;
    uint32_t ir_raw;
} ppg_sample_t;
```

---

## 7. Giao tiếp UART với máy tính

Máy tính gửi lệnh UART xuống ESP32 để bắt đầu đo.

Ví dụ lệnh:

```text
START 10
```

Ý nghĩa:

```text
START: bắt đầu đo
10: thời gian đo là 10 giây
```

ESP32 sau khi nhận lệnh sẽ:

```text
1. Xóa buffer cũ
2. Reset timer timestamp
3. Bắt đầu thu ECG và PPG
4. Dừng sau 10 giây
5. Gửi dữ liệu thô về máy tính
```

Có thể mở rộng thêm các lệnh:

```text
START <duration_s>     Bắt đầu đo trong duration_s giây
STOP                   Dừng đo thủ công
CONFIG PPG_SR <value>  Cấu hình sample rate PPG
CONFIG ECG_SR <value>  Cấu hình sample rate ECG
```

---

## 8. Định dạng file CSV

Sau khi ESP32 gửi dữ liệu thô qua UART, máy tính lưu dữ liệu thành file CSV.

Định dạng đề xuất:

```csv
time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw
0,2048,123456,120980
1,2050,,
2,2047,,
...
10,2060,123470,120995
```

Vì ECG và PPG có thể khác tần số lấy mẫu nên không phải dòng nào cũng có đủ cả ECG và PPG.

Ví dụ:

- ECG lấy mẫu 1000 Hz → mỗi 1 ms có một mẫu
- PPG lấy mẫu 100 Hz → mỗi 10 ms có một mẫu

Các mẫu được đồng bộ bằng cột `time_ms`.

---

## 9. Xử lý tín hiệu sau khi đo

File CSV chứa dữ liệu thô ban đầu. Sau đó có thể xử lý tín hiệu trên máy tính bằng Python hoặc MATLAB.

Các bước xử lý đề xuất:

```text
1. Đọc file CSV
2. Tách tín hiệu ECG và PPG
3. Loại bỏ giá trị trống
4. Lọc nhiễu
5. Chuẩn hóa tín hiệu
6. Phân tích đặc trưng
```

Ví dụ các bộ lọc có thể dùng:

```text
ECG:
- Bandpass filter
- Notch filter 50 Hz
- Wavelet denoising

PPG:
- Moving average
- Bandpass filter
- Wavelet denoising
- Loại bỏ baseline drift
```

---

## 10. Sơ đồ hệ thống

```text
             ┌────────────────────┐
             │      Máy tính       │
             │ UART START <time>   │
             └─────────┬──────────┘
                       │
                       ▼
             ┌────────────────────┐
             │       ESP32         │
             │ Timer 1000 Hz       │
             └───────┬─────┬──────┘
                     │     │
        ┌────────────┘     └────────────┐
        ▼                               ▼
┌───────────────┐               ┌────────────────┐
│ AD8232 ECG    │               │ MAX30102 PPG   │
│ ADC + DMA     │               │ FIFO + I2C     │
└───────┬───────┘               └───────┬────────┘
        │                               │
        ▼                               ▼
 ECG raw + timestamp            PPG raw + timestamp
        │                               │
        └───────────────┬───────────────┘
                        ▼
              Gửi dữ liệu UART
                        ▼
                 Lưu file CSV
                        ▼
              Lọc và xử lý tín hiệu
```

---

## 11. Kết luận

Phương pháp đồng bộ được sử dụng trong hệ thống là đồng bộ theo timestamp.

AD8232 được lấy mẫu trực tiếp bằng ADC với timer 1000 Hz. MAX30102 tự lấy mẫu theo sample rate riêng và dữ liệu được đọc từ FIFO. Mỗi mẫu ECG và PPG đều được gắn timestamp dựa trên cùng một timer nội bộ của ESP32.

Nhờ đó, sau khi lưu dữ liệu thô thành file CSV, hai tín hiệu có thể được căn chỉnh theo thời gian và đưa vào các thuật toán xử lý tín hiệu như lọc Wavelet, lọc thông dải, phát hiện đỉnh R ECG hoặc tính nhịp tim từ PPG.
